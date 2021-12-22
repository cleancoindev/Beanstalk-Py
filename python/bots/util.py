from abc import abstractmethod
from enum import Enum
import logging
import sys
import threading
import time


from web3 import eth

from data_access.graphs import (
    BeanSqlClient, BeanstalkSqlClient, LAST_PEG_CROSS_FIELD, PRICE_FIELD)
from data_access import eth_chain

# Configure uncaught exception handling for main and threads.
def log_exceptions(exc_type, exc_value, exc_traceback):
    """Log uncaught exceptions for main thread."""
    logging.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = log_exceptions
def log_thread_exceptions(args):
    """Log uncaught exceptions for threads."""
    logging.critical("Uncaught exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
threading.excepthook = log_thread_exceptions


# Strongly encourage Python 3.8+.
# If not 3.8+ uncaught exceptions on threads will not be logged.
MIN_PYTHON = (3, 8)
if sys.version_info < MIN_PYTHON:
    logging.critical(
        "Python %s.%s or later is required for proper exception logging.\n" % MIN_PYTHON)


TIMESTAMP_KEY = 'timestamp'
# There is a built in assumption that we will update at least once per
# Ethereum block (~13.5 seconds), so frequency should not be set too low.
PEG_UPDATE_FREQUENCY = 0.1  # hz
# The duration of a season. Assumes that seasons align with Unix epoch.
SEASON_DURATION = 3600  # seconds
# How long to wait between checks for a sunrise when we expect a new season to begin.
SUNRISE_CHECK_PERIOD = 10
# Rate at which to check chain for new Uniswap V2 pool interactions.
POOL_CHECK_RATE = 10 # seconds
# Rate at which to check for events on the Beanstalk contract.
BEANSTALK_CHECK_RATE = 10  # seconds
# Bytes in 50 megabytes.
FIFTY_MEGABYTES = 500**6
# Time to wait before restarting a monitor after an unhandled exception.
MONITOR_RESET_DELAY = 5

class PegCrossType(Enum):
    NO_CROSS = 0
    CROSS_ABOVE = 1
    CROSS_BELOW = 2


class Monitor():
    """Base class for monitors. Do not use directly.

    Args:
        name: simple human readable name string to use for logging.
        message_function: fun(str) style function to send application messages.
        query_rate: int representing rate monitored data should be queried (in seconds).
        prod: bool indicating if this is a production instance or not.
    """

    def __init__(self, name, message_function, query_rate, prod=False, dry_run=False):
        self.name = name
        self.message_function = message_function
        self.query_rate = query_rate
        self.prod = prod
        self._dry_run = dry_run
        self._thread_active = False
        self._thread_wrapper = threading.Thread(target=self._thread_wrapper_method)

    @abstractmethod
    def _monitor_method(self):
        pass

    def start(self):
        logging.info(f'Starting {self.name} monitoring thread...')
        if self._dry_run:
            self.message_function(f'{self.name} monitoring started (with simulated data).')
        elif not self.prod:
            self.message_function(f'{self.name} monitoring started.')
        self._thread_active = True
        self._thread_wrapper.start()

    def stop(self):
        if not self.prod:
            logging.info(f'Stopping {self.name} monitoring thread...')
        self._thread_active = False
        self._thread_wrapper.join(3 * self.query_rate)
        self.message_function(f'{self.name} monitoring stopped.')

    def _thread_wrapper_method(self):
        """
        Many of the web3 calls can fail arbitrarily on external calls. Gracefully log all
        excpetions and continue.
        """
        retry_time = 0
        while self._thread_active:
            if time.time() < retry_time:
                time.sleep(0.5)
                continue
            try:
                self._monitor_method()
            except Exception as e:
                logging.exception(f'Unhandled exception in the {self.name} thread.'
                                  f'\nLogging here and restarting monitor.')
                logging.exception(e)
            retry_time = time.time() + MONITOR_RESET_DELAY


class PegCrossMonitor(Monitor):
    """Monitor bean graph for peg crosses and send out messages on detection."""

    def __init__(self, message_function, prod=False):
        super().__init__('peg', message_function, 1 /
                         PEG_UPDATE_FREQUENCY, prod=prod, dry_run=False)
        self.bean_graph_client = BeanSqlClient()
        self.last_known_cross = None

    def _monitor_method(self):
        """Continuously monitor for BEAN price crossing the peg.

        Note that this assumes that block time > period of graph checks.
        """
        # Delay startup to protect against crash loops.
        min_update_time = time.time() + 1
        while self._thread_active:
            # Attempt to check as quickly as the graph allows, but no faster than set frequency.
            if not time.time() > min_update_time:
                time.sleep(1)
                continue
            min_update_time = time.time() + 1 / PEG_UPDATE_FREQUENCY

            cross_types = self._check_for_peg_crosses()
            for cross_type in cross_types:
                if cross_type != PegCrossType.NO_CROSS:
                    output_str = PegCrossMonitor.peg_cross_string(cross_type)
                    self.message_function(output_str)

    def _check_for_peg_crosses(self):
        """
        Check to see if the peg has been crossed since the last known timestamp of the caller.
        Assumes that block time > period of graph checks.

        Returns:
            [PegCrossType]
        """
        # Get latest data from graph.
        last_cross = self.bean_graph_client.last_cross()

        # # For testing.
        # import random
        # self.last_known_cross = {'timestamp': 1}
        # price = random.uniform(0.5, 1.5)

        # If the last known cross has not been set yet, initialize it.
        if not self.last_known_cross:
            logging.info('Peg cross timestamp initialized with last peg cross = '
                         f'{last_cross[TIMESTAMP_KEY]}')
            self.last_known_cross = last_cross
            return [PegCrossType.NO_CROSS]

        # If the cross is not newer than the last known cross, return.
        if last_cross[TIMESTAMP_KEY] <= self.last_known_cross[TIMESTAMP_KEY]:
            return [PegCrossType.NO_CROSS]

        # If multiple crosses have occurred since last known cross.
        number_of_new_crosses = int(last_cross['id']) - int(self.last_known_cross['id'])
        if number_of_new_crosses > 1:
            new_cross_list = self.bean_graph_client.get_last_crosses(n=number_of_new_crosses)
        else:
            new_cross_list = [last_cross]

        # Set the last known cross to be the latest new cross.
        self.last_known_cross = last_cross

        # At least one new cross has been detected. Determine the cross type and return.
        cross_types = []
        for cross in new_cross_list:
            if cross['above']:
                logging.info('Price crossed above peg.')
                cross_types.append(PegCrossType.CROSS_ABOVE)
            else:
                logging.info('Price crossed below peg.')
                cross_types.append(PegCrossType.CROSS_BELOW)
        return cross_types

    @abstractmethod
    def peg_cross_string(cross_type):
        """Return peg cross string used for bot messages."""
        # NOTE(funderberker): Have to compare enum values here because method of import of caller
        # can change the enum id.
        if cross_type.value == PegCrossType.CROSS_ABOVE.value:
            return '🟩↗ BEAN crossed above peg!'
        elif cross_type.value == PegCrossType.CROSS_BELOW.value:
            return '🟥↘ BEAN crossed below peg!'
        else:
            return 'Peg not crossed.'


class SunriseMonitor(Monitor):
    def __init__(self, message_function, prod=False):
        super().__init__('sunrise', message_function,
                         SUNRISE_CHECK_PERIOD, prod=prod, dry_run=False)
        self.beanstalk_graph_client = BeanstalkSqlClient()
        # Most recent season processed. Do not initialize.
        self.current_season_id = None

    def _monitor_method(self):
        while self._thread_active:
            # Wait until the eligible for a sunrise.
            self._wait_until_expected_sunrise()
            # Once the sunrise is complete, get the season stats.
            current_season_stats, last_season_stats = self._block_and_get_seasons_stats()
            # Report season summary to users.
            if current_season_stats:
                self.message_function(self.season_summary_string(
                    last_season_stats, current_season_stats))

            # # For testing.
            # current_season_stats, last_season_stats = self.beanstalk_graph_client.seasons_stats()
            # self.message_function(self.season_summary_string(last_season_stats, current_season_stats))
            # time.sleep(5)

    def _wait_until_expected_sunrise(self):
        """Wait until beanstalk is eligible for a sunrise call.

        Assumes sunrise timing cycle beings with Unix Epoch (1/1/1970 00:00:00 UTC).
        This is not exact since we do not bother with syncing local and graph time.
        """
        seconds_until_next_sunrise = SEASON_DURATION - time.time() % SEASON_DURATION
        sunrise_ready_timestamp = time.time() + seconds_until_next_sunrise
        loop_count = 0
        while self._thread_active and time.time() < sunrise_ready_timestamp:
            if loop_count % 60 == 0:
                logging.info(f'Blindly waiting {int((sunrise_ready_timestamp - time.time())/60)} '
                             'more minutes until expected sunrise.')
            loop_count += 1
            time.sleep(1)

    def _block_and_get_seasons_stats(self):
        """Blocks until sunrise is complete, then returns stats of current and previous season.

        Repeatedly makes graph calls to check sunrise status.
        """
        # TODO(funderberker): Put in max number of checks here before giving up and wait for
        # next sunrise.
        while self._thread_active:
            current_season_stats, last_season_stats = self.beanstalk_graph_client.seasons_stats()
            # If a new season is detected and sunrise was sufficiently recent.
            if (self.current_season_id != current_season_stats['id'] and
                    int(current_season_stats['timestamp']) > time.time() - SEASON_DURATION / 2):
                self.current_season_id = current_season_stats['id']
                logging.info(
                    f'New season detected with id {self.current_season_id}')
                return current_season_stats, last_season_stats
            time.sleep(SUNRISE_CHECK_PERIOD)
        return None, None

    def season_summary_string(self, last_season_stats, current_season_stats):
        new_farmable_beans = float(current_season_stats['newFarmableBeans'])
        new_harvestable_pods = float(
            current_season_stats['newHarvestablePods'])
        newMintedBeans = new_farmable_beans + new_harvestable_pods
        # newSoil = float(current_season_stats['newSoil'])
        new_deposited_lp = float(last_season_stats["newDepositedLP"])
        new_withdrawn_lp = float(last_season_stats["newWithdrawnLP"])
        pooled_eth = float(current_season_stats['pooledEth'])
        pooled_beans = float(current_season_stats['pooledBeans'])
        total_lp = float(current_season_stats['lp'])
        # bean_pool_ratio = pooled_beans / total_lp
        # eth_pool_ratio = pooled_eth / total_lp
        # deposited_bean_lp = round_num(new_deposited_lp * bean_pool_ratio)
        # deposited_eth_lp = round_num(new_deposited_lp * eth_pool_ratio)
        deposited_eth_lp, deposited_bean_lp = lp_eq_values(
            new_deposited_lp, total_lp=total_lp, pooled_eth=pooled_eth, pooled_beans=pooled_beans)
        # withdrawn_bean_lp = round_num(new_withdrawn_lp * bean_pool_ratio)
        # withdrawn_eth_lp = round_num(new_withdrawn_lp * eth_pool_ratio)
        withdrawn_eth_lp, withdrawn_bean_lp = lp_eq_values(
            new_withdrawn_lp, total_lp=total_lp, pooled_eth=pooled_eth, pooled_beans=pooled_beans)
        last_weather = float(last_season_stats['weather'])
        newPods = float(last_season_stats['newPods'])

        ret_string = f'⏱ Season {last_season_stats["id"]} is complete!'
        ret_string += f'\n💵 The TWAP last season was ${round_num(current_season_stats["price"], 3)}'
        ret_string += f'\n🌤 The weather is {current_season_stats["weather"]}%'
        # ret_string += f'\nThere is {current_season_stats["soil"]} soil available' # Coming in graph version 1.1.10
        if newMintedBeans:
            ret_string += f'\n\n🌱 {round_num(newMintedBeans)} Beans were minted'
            ret_string += f'\n👩‍🌾 {round_num(new_farmable_beans)} Beans are newly farmable'
            ret_string += f'\n👨‍🌾 {round_num(new_harvestable_pods)} Pods are newly harvestable'
        else:
            ret_string += f'\n\n🌱 No new Beans were minted.'
        # if newSoil:
        #     ret_string += f'\n\n{round_num(newSoil)} soil was added'
        ret_string += f'\n\n📥 {round_num(last_season_stats["newDepositedBeans"])} Beans deposited'
        ret_string += f'\n📥 {round_num(deposited_bean_lp)} Beans and {round_num(deposited_eth_lp)} ETH of LP deposited'
        ret_string += f'\n📤 {round_num(last_season_stats["newWithdrawnBeans"])} Beans withdrawn'
        ret_string += f'\n📤 {round_num(withdrawn_bean_lp)} Beans and {round_num(withdrawn_eth_lp)} ETH of LP withdrawn'
        ret_string += f'\n🚜 {round_num(newPods / (1 + last_weather/100))} Beans sown'
        ret_string += f'\n🌾 {round_num(newPods)} Pods minted'
        ret_string += '\n_ _'  # empty line that does not get stripped
        return ret_string


class PoolMonitor(Monitor):
    """Monitor the ETH:BEAN Uniswap V2 pool for events."""

    def __init__(self, message_function, prod=False, dry_run=False):
        super().__init__('pool', message_function,
                         POOL_CHECK_RATE, prod=prod, dry_run=dry_run)
        self._eth_event_client = eth_chain.EthEventsClient(
            eth_chain.EventClientType.POOL)
        self.blockchain_client = eth_chain.BlockchainClient()

    def _monitor_method(self):
        last_check_time = 0
        while self._thread_active:
            if time.time() < last_check_time + POOL_CHECK_RATE:
                time.sleep(0.5)
                continue
            last_check_time = time.time()
            for txn_hash, event_logs in self._eth_event_client.get_new_logs(dry_run=self._dry_run).items():
                self._handle_txn_logs(txn_hash, event_logs)

    def _handle_txn_logs(self, txn_hash, event_logs):
        """Process the pool event logs for a single txn.

        Assumes that there are not non-Bean swaps in logs (e.g. ETH:USDC).
        Note that Event Log Object is not the same as Event object. *sideeyes web3.py developers.*
        """
        # Match the txn invoked method. Matching is done on the first 10 characters of the hash.
        transaction = self.blockchain_client._web3.eth.get_transaction(txn_hash)
        txn_method_sig_prefix = transaction['input'][:9]

        # Process the txn logs based on the method.
        # Ignore silo conversion events. They will be handled by the beanstalk class.
        if multi_sig_compare(txn_method_sig_prefix, eth_chain.silo_conversion_sigs):
            return
        # No special logic for deposits. If they include a swap we should process it as normal.
        elif multi_sig_compare(txn_method_sig_prefix, eth_chain.bean_deposit_sigs):
            pass
        else:
            # All other txn log sets should include a standard ETH:BEAN swap.
            pass

        # Each txn of interest should only include one ETH:BEAN swap.
        if len(event_logs) > 1:
            logging.warning(f'Multiple swaps of interest seen in a single txn ({str(event_logs)}).')
        for event_log in event_logs:
            event_str = default_pool_event_str(event_log, self.blockchain_client)
            if event_str:
                self.message_function(event_str)


def default_pool_event_str(event_log, blockchain_client):
    event_str = ''
    # Parse possible values of interest from the event log. Not all will be populated.
    eth_amount = eth_chain.eth_to_float(event_log.args.get('amount0'))
    bean_amount = eth_chain.bean_to_float(event_log.args.get('amount1'))
    eth_in = eth_chain.eth_to_float(event_log.args.get('amount0In'))
    eth_out = eth_chain.eth_to_float(event_log.args.get('amount0Out'))
    bean_in = eth_chain.bean_to_float(event_log.args.get('amount1In'))
    bean_out = eth_chain.bean_to_float(event_log.args.get('amount1Out'))

    # Get pricing from uni pools.
    eth_price, bean_price = blockchain_client.current_eth_and_bean_price()

    if event_log.event in ['Mint', 'Burn']:
        if event_log.event == 'Mint':
            event_str += f'📥 LP added - {round_num(bean_amount)} Beans and {round_num(eth_amount, 4)} ETH'
        if event_log.event == 'Burn':
            event_str += f'📤 LP removed - {round_num(bean_amount)} Beans and {round_num(eth_amount, 4)} ETH'
        # LP add/remove always takes equal value of both assets.
        lp_value = bean_amount * bean_price * 2
        event_str += f' (${round_num(lp_value)})'
        event_str += f'\n{value_to_emojis(lp_value)}'
    elif event_log.event == 'Swap':
        if eth_in > 0:
            event_str += f'📗 {round_num(bean_out)} Beans bought for {round_num(eth_in, 4)} ETH'
            swap_price = blockchain_client.avg_swap_price(
                eth_in, bean_out, eth_price=eth_price)
            swap_value = swap_price * bean_out
        elif bean_in > 0:
            event_str += f'📕 {round_num(bean_in)} Beans sold for {round_num(eth_out, 4)} ETH'
            swap_price = blockchain_client.avg_swap_price(
                eth_out, bean_in, eth_price=eth_price)
            swap_value = swap_price * bean_in
        else:
            logging.critical('Unexpected Swap args detected.')
            return ''
        event_str += f' @ ${round_num(swap_price, 4)} (${round_num(swap_value)})'
        event_str += f'  -  Latest block price is ${round_num(bean_price, 4)}'
        event_str += f'\n{value_to_emojis(swap_value)}'

    event_str += f'\n<https://etherscan.io/tx/{event_log.transactionHash.hex()}>'
    # empty line that does not get stripped
    event_str += '\n_ _'
    return event_str

class BeanstalkMonitor(Monitor):
    """Monitor the Beanstalk contract for events."""

    def __init__(self, message_function, prod=False, dry_run=False):
        super().__init__('beanstalk', message_function,
                         BEANSTALK_CHECK_RATE, prod=prod, dry_run=dry_run)
        self._eth_event_client = eth_chain.EthEventsClient(eth_chain.EventClientType.BEANSTALK)
        self.beanstalk_graph_client = BeanstalkSqlClient()
        self.blockchain_client = eth_chain.BlockchainClient()

    def _monitor_method(self):
        last_check_time = 0
        while self._thread_active:
            if time.time() < last_check_time + BEANSTALK_CHECK_RATE:
                time.sleep(0.5)
                continue
            last_check_time = time.time()
            for txn_hash, event_logs in self._eth_event_client.get_new_logs(dry_run=self._dry_run).items():
                self._handle_txn_logs(txn_hash, event_logs)

    def _handle_txn_logs(self, txn_hash, event_logs):
        """Process the beanstalk event logs for a single txn.

        Note that Event Log Object is not the same as Event object.
        """
        # Match the txn invoked method. Matching is done on the first 10 characters of the hash.
        transaction = self.blockchain_client._web3.eth.get_transaction(txn_hash)
        txn_method_sig_prefix = transaction['input'][:9]

        # Prune embedded bean deposit logs. They are uninteresting clutter.
        last_bean_deposit = None
        bean_deposit_logs = []
        for event_log in event_logs:
            if event_log.event == 'BeanDeposit':
                if last_bean_deposit is None or event_log.logIndex > last_bean_deposit.logIndex:
                    last_bean_deposit = event_log
                bean_deposit_logs.append(event_log)
        # Remove all bean_deposit logs from the log list.
        for event_log in bean_deposit_logs:
            # Remove this log from the list.
            event_logs.remove(event_log)

        # Process the txn logs based on the method.
        # Compile all events within a silo conversion to a single action.
        if multi_sig_compare(txn_method_sig_prefix, eth_chain.silo_conversion_sigs):
            logging.info(f'Silo conversion txn seen ({txn_hash.hex()}).')
            # Include last bean deposit log for this type of txn.
            event_logs.append(last_bean_deposit)
            self.message_function(silo_conversion_str(event_logs, self.blockchain_client, self.beanstalk_graph_client))
            return
        # If there is a direct bean deposit, do not ignore the last bean deposit event.
        elif multi_sig_compare(txn_method_sig_prefix, eth_chain.bean_deposit_sigs):
            logging.info(f'Bean deposit txn seen ({txn_hash.hex()}).')
            # Include last bean deposit log for this type of txn.
            event_logs.append(last_bean_deposit)

        # Handle txn logs individually using default strings.
        for event_log in event_logs:
            event_str = default_beanstalk_event_str(event_log, self.blockchain_client,
                                                    self.beanstalk_graph_client)
            self.message_function(event_str)

def default_beanstalk_event_str(event_log, blockchain_client, beanstalk_graph_client):
        event_str = ''

        eth_price, bean_price = blockchain_client.current_eth_and_bean_price()
        lp_amount = eth_chain.lp_to_float(event_log.args.get('lp'))
        lp_eth, lp_beans = lp_eq_values(
            lp_amount, beanstalk_graph_client=beanstalk_graph_client)
        lp_value = lp_eth * eth_price + lp_beans * bean_price
        beans_amount = eth_chain.bean_to_float(event_log.args.get('beans'))
        beans_value = beans_amount * bean_price
        pods_amount = eth_chain.bean_to_float(event_log.args.get('pods'))

        # Ignore these events. They are uninteresting clutter.
        if event_log.event in ['BeanRemove', 'LPRemove']:
            return ''
        # LP Events.
        elif event_log.event in ['LPDeposit', 'LPWithdraw', 'LPClaim']:
            if event_log.event == 'LPDeposit':
                event_str += f'📥 LP deposited'
            elif event_log.event == 'LPWithdraw':
                event_str += f'📭 LP withdrawn'
            elif event_log.event == 'LPClaim':
                event_str += f'🛍 LP claimed'
            event_str += f' - {round_num(lp_beans)} Beans and {round_num(lp_eth,4)} ETH (${round_num(lp_value)})'
            event_str += f'\n{value_to_emojis(lp_value)}'
        # Bean events.
        elif event_log.event in ['BeanDeposit', 'BeanWithdraw', 'BeanClaim']:
            if event_log.event == 'BeanDeposit':
                event_str += f'📥 Beans deposited'
            elif event_log.event == 'BeanWithdraw':
                event_str += f'📭 Beans withdrawn'
            elif event_log.event == 'BeanClaim':
                event_str += f'🛍 Beans claimed'
            event_str += f' - {round_num(beans_amount)} Beans (${round_num(beans_value)})'
            event_str += f'\n{value_to_emojis(beans_value)}'
        # Sow event.
        elif event_log.event == 'Sow':
            event_str += f'🚜 {round_num(beans_amount)} Beans sown for ' \
                         f'{round_num(pods_amount)} Pods (${round_num(beans_value)})'
            event_str += f'\n{value_to_emojis(beans_value)}'
        else:
            logging.warning(
                f'Unexpected event log from Beanstalk contract ({event_log}). Ignoring.')
            return ''

        event_str += f'\n<https://etherscan.io/tx/{event_log.transactionHash.hex()}>'
        # empty line that does not get stripped
        event_str += '\n_ _'
        return event_str

def silo_conversion_str(event_logs, blockchain_client, beanstalk_graph_client):
    """Create a human-readable string representing a silo position conversion.
    
    Assumes that there are no non-Bean swaps contained in the event logs.
    Assumes event_logs is not empty.
    Uses events from Beanstalk contract.
    """
    beans_converted = lp_converted = None
    bean_price = blockchain_client.current_bean_price()
    # Find the relevant logs (Swap + Mint/Burn).
    for event_log in event_logs:
        # One Swap event will always be present.
        # if event_log.event == 'Swap':
        #     eth_in = eth_chain.eth_to_float(event_log.args.get('amount0In'))
        #     eth_out = eth_chain.eth_to_float(event_log.args.get('amount0Out'))
        #     bean_in = eth_chain.bean_to_float(event_log.args.get('amount1In'))
        #     bean_out = eth_chain.bean_to_float(event_log.args.get('amount1Out'))

        # One of the below two events will always be present.
        if event_log.event == 'BeanRemove':
            beans_converted = eth_chain.bean_to_float(event_log.args.get('beans'))
            value = beans_converted * bean_price
        elif event_log.event == 'LPRemove':
            lp_converted = eth_chain.lp_to_float(event_log.args.get('lp'))
            lp_converted_eth, lp_converted_beans = lp_eq_values(lp_converted, beanstalk_graph_client=beanstalk_graph_client)

        # One of the below two events will always be present.
        if event_log.event == 'BeanDeposit':
            beans_deposited = eth_chain.bean_to_float(event_log.args.get('beans'))
            value = beans_deposited * bean_price
        elif event_log.event == 'LPDeposit':
            lp_deposited = eth_chain.lp_to_float(event_log.args.get('lp'))
            lp_deposited_eth, lp_deposited_beans = lp_eq_values(lp_deposited, beanstalk_graph_client=beanstalk_graph_client)
            value = lp_deposited_beans * 2 * bean_price

        # elif event_log.event == 'Burn':
        # elif event_log.event == 'Mint':

    # If converting to LP.
    if beans_converted:
        event_str = f'🔃 {round_num(beans_converted)} siloed Beans converted to {round_num(lp_deposited_eth,4)} ETH & {round_num(lp_deposited_beans)} Beans of LP (${round_num(value)})'

    # If converting to Beans.
    elif lp_converted:
        event_str = f'🔄 {round_num(lp_converted_eth,4)} ETH and {round_num(lp_converted_beans)} Beans of siloed LP converted to {round_num(beans_deposited)} siloed Beans (${round_num(value)})'

    event_str += f'\n{value_to_emojis(value)}'
    event_str += f'\n<https://etherscan.io/tx/{event_logs[0].transactionHash.hex()}>'
    return event_str

def multi_sig_compare(signature, signatures):
    """Compare signature to all signatures in list and return if there are any matches. 

    Comparison is made based on 10 character prefix.
    """
    for sig in signatures:
        if signature[:9] == sig[:9]:
            return True
    return False

def lp_eq_values(lp, total_lp=None, pooled_eth=None, pooled_beans=None, beanstalk_graph_client=None):
    """Return the amount of ETH and beans equivalent to an amount of LP.

    Args:
        total_lp: current amount of lp in pool.
        pooled_eth: current amount of eth in pool.
        pooled_beans: current amount of beans in pool.
        beanstalk_graph_client: a beanstalk graphsql client. If provided latest season stats will
            be retrieved and used.
    """
    if beanstalk_graph_client:
        current_season_stats = beanstalk_graph_client.current_season_stats()
        pooled_eth = float(current_season_stats['pooledEth'])
        pooled_beans = float(current_season_stats['pooledBeans'])
        total_lp = float(current_season_stats['lp'])

    if None in [total_lp, pooled_eth, pooled_beans]:
        raise ValueError(
            'Must provide (total_lp & pooled_eth & pooled_beans) OR beanstalk_graph_client')

    bean_pool_ratio = pooled_beans / total_lp
    eth_pool_ratio = pooled_eth / total_lp
    eth_lp = lp * eth_pool_ratio
    bean_lp = lp * bean_pool_ratio
    return eth_lp, bean_lp


def round_num(number, precision=2):
    """Round a string or float to requested precision and return as a string."""
    return f'{float(number):,.{precision}f}'


def value_to_emojis(value):
    """Convert a rounded dollar value to a string of emojis."""
    value = int(value)
    if value < 0:
        return ''
    value = round(value, -3)
    if value < 10000:
        return '🐟' * (value // 1000) or '🐟'
    value = round(value, -4)
    if value < 100000:
        return '🦈' * (value // 10000)
    value = round(value, -5)
    return '🐳' * (value // 100000)


def msg_includes_embedded_links(msg):
    """Attempt to detect if there are embedded links in this message. Not an exact system."""
    if msg.count(']('):
        return True


def handle_sigterm(signal_number, stack_frame):
    """Process a sigterm with a python exception for clean exiting."""
    logging.warning("Handling SIGTERM. Exiting.")
    raise SystemExit

if __name__ == '__main__':
    """Quick test and demonstrate functionality."""
    logging.basicConfig(level=logging.INFO)

    sunrise_monitor = SunriseMonitor(print)
    sunrise_monitor.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    sunrise_monitor.stop()