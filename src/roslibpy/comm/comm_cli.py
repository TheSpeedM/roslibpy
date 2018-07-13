from __future__ import print_function

import logging
import math

from System import Action, Array, ArraySegment, Byte, TimeSpan, Uri
from System.Net.WebSockets import ClientWebSocket, WebSocketCloseStatus, WebSocketMessageType, WebSocketReceiveResult, WebSocketState
from System.Text import Encoding
from System.Threading import CancellationToken, CancellationTokenSource, ManualResetEventSlim, SemaphoreSlim, Thread
from System.Threading.Tasks import Task

from . import RosBridgeException, RosBridgeProtocol
from ..event_emitter import EventEmitterMixin

LOGGER = logging.getLogger('roslibpy')
RECEIVE_CHUNK_SIZE = 1024
SEND_CHUNK_SIZE = 1024


class CliRosBridgeProtocol(RosBridgeProtocol):
    """Implements the ROS Bridge protocol on top of CLI WebSockets.

    This implementation is mainly intended to be used on IronPython
    implementations and makes use of the Tasks library of .NET for
    most internal scheduling and cancellation signals."""
    def __init__(self, factory, socket, *args, **kwargs):
        super(CliRosBridgeProtocol, self).__init__(*args, **kwargs)
        self.factory = factory
        self.socket = socket
        self.semaphore = SemaphoreSlim(1)

    def on_open(self, task):
        """Triggered when the socket connection has been established.

        This will kick-start the listening thread."""
        LOGGER.info('Connection to ROS MASTER ready.')

        self.factory.ready(self)
        self.factory.manager.call_in_thread(self.start_listening)

    def receive_chunk_async(self, task_result, context):
        """Handle the reception of a message chuck asynchronously."""
        try:
            if task_result:
                result = task_result.Result

                # NOTE: If we're not at the end of the message
                # we will enter the lock (Semaphore), to make sure we're
                # exclusively accessing the socket read/writes
                if not result.EndOfMessage:
                    self.semaphore.Wait(
                        self.factory.manager.cancellation_token)

                if result.MessageType == WebSocketMessageType.Close:
                    LOGGER.info('WebSocket connection closed: [Code=%s] Description=%s',
                                result.CloseStatus, result.CloseStatusDescription)
                    return self.send_close()
                else:
                    chunk = Encoding.UTF8.GetString(context['buffer'], 0, result.Count)
                    context['content'].append(chunk)

                    # Signal the listener thread if we're done parsing chunks
                    if result.EndOfMessage:
                        # NOTE: Once we reach the end of the message
                        # we release the lock (Semaphore)
                        self.semaphore.Release()

                        # And signal the manual reset event
                        context['mre'].Set()
                        return task_result

            receive_task = self.socket.ReceiveAsync(ArraySegment[Byte](
                context['buffer']), self.factory.manager.cancellation_token)
            receive_task.ContinueWith.Overloads[Action[Task[WebSocketReceiveResult], object], object](
                self.receive_chunk_async, context)

        except Exception:
            LOGGER.exception('Exception on receive_chunk_async, processing will be aborted')
            if task_result and task_result.Exception:
                LOGGER.debug('Inner exception: %s', task_result.Exception)
            raise

    def start_listening(self):
        """Starts listening asynchronously while the socket is open.

        The inter-thread synchronization between this and the async
        reception threads is sync'd with a manual reset event."""
        try:
            LOGGER.debug(
                'About to start listening, socket state: %s', self.socket.State)

            while self.socket and self.socket.State == WebSocketState.Open:
                mre = ManualResetEventSlim(False)
                content = []
                buffer = Array.CreateInstance(Byte, RECEIVE_CHUNK_SIZE)

                self.receive_chunk_async(None, dict(
                    buffer=buffer, content=content, mre=mre))

                LOGGER.debug('Waiting for messages...')
                mre.Wait(self.factory.manager.cancellation_token)

                try:
                    message_payload = ''.join(content)
                    LOGGER.debug('Message reception completed|<pre>%s</pre>', message_payload)
                    self.on_message(message_payload)
                except Exception:
                    LOGGER.exception('Exception on start_listening while trying to handle message received.' +
                                     'It could indicate a bug in user code on message handlers. Message skipped.')
        except Exception:
            LOGGER.exception(
                'Exception on start_listening, processing will be aborted')
            raise

    def send_close(self):
        """Trigger the closure of the websocket indicating normal closing process.

        This disposes the socket completely after disconnection,
        assuming this is an user-requested disconnect."""
        if self.socket:
            close_task = self.socket.CloseAsync(
                WebSocketCloseStatus.NormalClosure, '', CancellationToken.None)

            # NOTE: Make sure reconnets are possible.
            # Reconnection needs to be handled on a higher layer.
            close_task.ContinueWith(self.dispose)
            return close_task

    def send_chunk_async(self, task_result, message_data):
        """Send a message chuck asynchronously."""
        try:
            message_buffer, message_length, chunks_count, i = message_data

            offset = SEND_CHUNK_SIZE * i
            is_last_message = (i == chunks_count - 1)

            if is_last_message:
                count = message_length - offset
            else:
                count = SEND_CHUNK_SIZE

            message_chunk = ArraySegment[Byte](message_buffer, offset, count)
            LOGGER.debug('Chunk %d of %d|From offset=%d, byte count=%d, Is last=%s',
                         i + 1, chunks_count, offset, count, str(is_last_message))
            task = self.socket.SendAsync(
                message_chunk, WebSocketMessageType.Text, is_last_message, self.factory.manager.cancellation_token)

            if not is_last_message:
                task.ContinueWith(self.send_chunk_async, [
                    message_buffer, message_length, chunks_count, i + 1])
            else:
                # NOTE: If we've reached the last chunck of the message
                # we can release the lock (Semaphore) again.
                self.semaphore.Release()

            return task
        except Exception:
            LOGGER.exception('Exception while on send_chunk_async')
            raise

    def send_message(self, payload):
        """Start sending a message over the websocket asynchronously."""

        if self.socket.State != WebSocketState.Open:
            raise RosBridgeException(
                'Connection is not open. Socket state: %s' % self.socket.State)

        try:
            # NOTE: Before we start sending a message
            # we will enter the lock (Semaphore), to make sure we're
            # exclusively accessing the socket read/writes
            self.semaphore.Wait(self.factory.manager.cancellation_token)

            message_buffer = Encoding.UTF8.GetBytes(payload)
            message_length = len(message_buffer)
            chunks_count = int(math.ceil(float(message_length) / SEND_CHUNK_SIZE))

            send_task = self.send_chunk_async(
                None, [message_buffer, message_length, chunks_count, 0])

            return send_task
        except Exception:
            LOGGER.exception('Exception while sending message')
            raise

    def dispose(self, *args):
        """Dispose the resources held by this protocol instance, i.e. socket."""
        self.factory.manager.trigger_disconnect()

        if self.factory.manager.cancellation_token_source:
            LOGGER.debug('Cancelling task token')
            self.factory.manager.cancellation_token_source.Cancel()

        if self.socket:
            self.socket.Dispose()
            self.socket = None
            LOGGER.debug('Websocket disposed')

    def __del__(self):
        """Dispose correctly the connection."""
        self.dispose()


class CliRosBridgeClientFactory(EventEmitterMixin):
    """Factory to create instances of the ROS Bridge protocol built on top of .NET WebSockets."""

    def __init__(self, url, *args, **kwargs):
        super(CliRosBridgeClientFactory, self).__init__(*args, **kwargs)
        self._manager = CliEventLoopManager()
        self.proto = None
        self.url = Uri(url)

    @property
    def is_connected(self):
        """Indicate if the WebSocket connection is open or not.

        Returns:
            bool: True if WebSocket is connected, False otherwise.
        """
        return self.proto and self.proto.socket and self.proto.socket.State == WebSocketState.Open

    def connect(self):
        """Establish WebSocket connection to the ROS server defined for this factory.

        Returns:
            async_task: The async task for the connection.
        """
        LOGGER.debug('Started to connect...')
        socket = ClientWebSocket()
        socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(5)
        connect_task = socket.ConnectAsync(
            self.url, self.manager.cancellation_token)

        protocol = CliRosBridgeProtocol(self, socket)
        connect_task.ContinueWith(protocol.on_open)

        return connect_task

    def ready(self, proto):
        self.proto = proto
        self.emit('ready', proto)

    def on_ready(self, callback):
        if self.proto:
            callback(self.proto)
        else:
            self.once('ready', callback)

    @property
    def manager(self):
        """Get an instance of the event loop manager for this factory."""
        return self._manager


class CliEventLoopManager(object):
    """Manage the main event loop using .NET threads.

    For the time being, this implementation is pretty light
    and mostly relies on .NET async doing "the right thing(tm)"
    with a sprinkle of threading here and there.
    """

    def __init__(self):
        self.cancellation_token_source = CancellationTokenSource()
        self.cancellation_token = self.cancellation_token_source.Token
        self._disconnect_event = ManualResetEventSlim(False)

    def run_forever(self):
        """Kick-starts a blocking loop while the ROS client is connected."""
        self._disconnect_event.Wait(self.cancellation_token)
        LOGGER.debug('Received disconnect event on main loop')

    def trigger_disconnect(self):
        """Internal: used by the protocol to signal disconnection on the main loop."""
        self._disconnect_event.Set()

    def call_later(self, delay, callback):
        """Call the given function after a certain period of time has passed.

        Args:
            delay (:obj:`int`): Number of seconds to wait before invoking the callback.
            callback (:obj:`callable`): Callable function to be invoked when the delay has elapsed.
        """
        # NOTE: Maybe there's a more elegant way of doing this
        def closure():
            Thread.Sleep(delay * 1000)
            callback()

        Task.Factory.StartNew(closure, self.cancellation_token)

    def call_in_thread(self, callback):
        """Call the given function on a thread.

        Args:
            callback (:obj:`callable`): Callable function to be invoked in a thread.
        """
        Task.Factory.StartNew(callback, self.cancellation_token)

    def terminate(self):
        """Signals the termination of the main event loop."""
        self.cancellation_token_source.Cancel(self.cancellation_token)
