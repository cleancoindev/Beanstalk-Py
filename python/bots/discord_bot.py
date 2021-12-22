from enum import Enum
import logging
import logging.handlers
import signal
import os

import discord
from discord.ext import tasks, commands

from bots import util


DISCORD_CHANNEL_ID_PEG_CROSSES = 911338190198169710
DISCORD_CHANNEL_ID_SEASONS = 911338078080221215
DISCORD_CHANNEL_ID_POOL = 915372733758603284
DISCORD_CHANNEL_ID_BEANSTALK = 918240659914227713
DISCORD_CHANNEL_ID_TEST_BOT = 908035718859874374


class DiscordClient(discord.ext.commands.Bot):

    class Channel(Enum):
        PEG = 0
        SEASONS = 1
        POOL = 2
        BEANSTALK = 3

    def __init__(self, prod=False):
        super().__init__(command_prefix=commands.when_mentioned_or("!"))

        if prod:
            self._chat_id_peg = DISCORD_CHANNEL_ID_PEG_CROSSES
            self._chat_id_seasons = DISCORD_CHANNEL_ID_SEASONS
            self._chat_id_pool = DISCORD_CHANNEL_ID_POOL
            self._chat_id_beanstalk = DISCORD_CHANNEL_ID_BEANSTALK
            logging.info('Configured as a production instance.')
        else:
            self._chat_id_peg = DISCORD_CHANNEL_ID_TEST_BOT
            self._chat_id_seasons = DISCORD_CHANNEL_ID_TEST_BOT
            self._chat_id_pool = DISCORD_CHANNEL_ID_TEST_BOT
            self._chat_id_beanstalk = DISCORD_CHANNEL_ID_TEST_BOT
            logging.info('Configured as a staging instance.')

        self.msg_queue = []

        self.peg_cross_monitor = util.PegCrossMonitor(
            self.send_msg_peg, prod=prod)
        self.peg_cross_monitor.start()

        self.sunrise_monitor = util.SunriseMonitor(
            self.send_msg_seasons, prod=prod)
        self.sunrise_monitor.start()

        self.pool_monitor = util.PoolMonitor(self.send_msg_pool, prod=prod)
        self.pool_monitor.start()

        self.beanstalk_monitor = util.BeanstalkMonitor(self.send_msg_beanstalk, prod=prod)
        self.beanstalk_monitor.start()

        # Start the message queue sending task in the background.
        self.send_queued_messages.start()

    def stop(self):
        self.peg_cross_monitor.stop()
        self.sunrise_monitor.stop()
        self.pool_monitor.stop()
        self.beanstalk_monitor.stop()

    def send_msg_peg(self, text):
        """Send a message through the Discord bot in the peg channel."""
        self.msg_queue.append((self.Channel.PEG, text))

    def send_msg_seasons(self, text):
        """Send a message through the Discord bot in the seasons channel."""
        self.msg_queue.append((self.Channel.SEASONS, text))

    def send_msg_pool(self, text):
        """Send a message through the Discord bot in the pool channel."""
        self.msg_queue.append((self.Channel.POOL, text))

    def send_msg_beanstalk(self, text):
        """Send a message through the Discord bot in the beanstalk channel."""
        self.msg_queue.append((self.Channel.BEANSTALK, text))

    async def on_ready(self):
        self._channel_peg = self.get_channel(self._chat_id_peg)
        self._channel_seasons = self.get_channel(
            self._chat_id_seasons)
        self._channel_pool = self.get_channel(self._chat_id_pool)
        self._channel_beanstalk = self.get_channel(self._chat_id_beanstalk)
        logging.info(
            f'Discord channels are {self._channel_peg}, {self._channel_seasons}, '
            f'{self._channel_pool}, {self._channel_beanstalk}')

    @tasks.loop(seconds=0.1, reconnect=True)
    async def send_queued_messages(self):
        """Send messages in queue."""
        for channel, msg in self.msg_queue:
            if channel is self.Channel.PEG:
                await self._channel_peg.send(msg)
            elif channel is self.Channel.SEASONS:
                await self._channel_seasons.send(msg)
            elif channel is self.Channel.POOL:
                await self._channel_pool.send(msg)
            elif channel is self.Channel.BEANSTALK:
                await self._channel_beanstalk.send(msg)
            else:
                logging.error('Unknown channel seen in msg queue: {channel}')
            self.msg_queue = self.msg_queue[1:]
            logging.info(f'Message sent through {channel.name} channel:\n{msg}\n')

    @send_queued_messages.before_loop
    async def before_send_queued_messages_loop(self):
        """Wait until the bot logs in."""
        await self.wait_until_ready()

    async def on_message(self, message):
        """Respond to messages."""
        # Do not reply to itself.
        if message.author.id == self.user.id:
            return

        # Process commands.
        await self.process_commands(message)


def configure_bot_commands(bot):
    @bot.command(pass_context=True)
    async def botstatus(ctx):
        await ctx.send('I am alive and running!')


if __name__ == '__main__':
    logging.basicConfig(format='Discord Bot : %(levelname)s : %(asctime)s : %(message)s',
                        level=logging.INFO, handlers=[
                            logging.handlers.RotatingFileHandler("discord_bot.log",
                                                                 maxBytes=util.FIFTY_MEGABYTES),
                            logging.StreamHandler()])
    signal.signal(signal.SIGTERM, util.handle_sigterm)

    # Automatically detect if this is a production environment.
    try:
        token = os.environ["DISCORD_BOT_TOKEN_PROD"]
        prod = True
    except KeyError:
        token = os.environ["DISCORD_BOT_TOKEN"]
        prod = False

    discord_client = DiscordClient(prod=prod)
    configure_bot_commands(discord_client)

    try:
        discord_client.run(token)
    except (KeyboardInterrupt, SystemExit):
        pass
    # Note that discord bot cannot send shutting down messages in its channel, due to lib impl.
    discord_client.stop()