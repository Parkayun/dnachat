# -*-coding:utf8-*-
import bson
import datetime
import json
import redis
import threading
import time

from boto import sqs
from boto.sqs.message import Message as QueueMessage
from bynamodb.exceptions import ItemNotFoundException
from twisted.internet.protocol import Factory
from twisted.internet.threads import deferToThread

from .decorators import in_channel_required, auth_required
from .dna.protocol import DnaProtocol, ProtocolError
from .logger import logger
from .transmission import Transmitter
from .settings import conf
from .models import (Message as DnaMessage, Channel, ChannelJoinInfo,
                     ChannelWithdrawalLog, ChannelUsageLog)


class BaseChatProtocol(DnaProtocol):
    def __init__(self):
        self.user = None
        self.protocol_version = None
        self.attended_channel_join_info = None
        self.status = 'pending'
        self.pending_messages = []
        self.pending_messages_lock = threading.Lock()

    def requestReceived(self, request):
        processor = getattr(self, 'do_%s' % request.method, None)
        if processor is None:
            raise ProtocolError('Unknown method')
        processor(request)

    def do_authenticate(self, request):
        self.user = self.authenticate(request)
        if self.user is None:
            raise ProtocolError('Authentication failed')
        self.user.id = str(self.user.id).decode('utf8')
        self.user.join_infos = list(ChannelJoinInfo.by_user(self.user.id))
        self.protocol_version = request.get('protocol_version')
        for join_info in self.user.join_infos:
            self.factory.channels.setdefault(join_info.channel, []).append(self)
        self.transport.write(bson.dumps(dict(method=u'authenticate', status=u'OK')))

    @auth_required
    def do_create(self, request):
        def main():
            if 'partner_id' in request:
                channel_names = [join_info.channel for join_info in self.user.join_infos]
                d = deferToThread(get_from_exists_private_channel, channel_names, request['partner_id'])
                chat_members = [self.user.id, request['partner_id']]
                d.addErrback(create_channel, chat_members, False)
            else:
                chat_members = [self.user.id] + request['partner_ids']
                d = deferToThread(create_channel, None, chat_members, True)
            d.addCallback(send_channel, [m for m in chat_members if m != self.user.id])

        def get_from_exists_private_channel(channel_names, partner_id):
            is_group_chat = dict(
                (channel.name, channel.is_group_chat)
                for channel in Channel.batch_get(*[(name,)for name in channel_names])
            )
            for join_info in [join_info for channel in channel_names
                              for join_info in ChannelJoinInfo.by_channel(channel)]:
                if join_info.user_id == partner_id and not is_group_chat[join_info.channel]:
                    return Channel.get_item(join_info.channel)
            raise ItemNotFoundException

        def create_channel(err, user_ids, is_group_chat):
            channel, join_infos = Channel.create_channel(user_ids, is_group_chat)
            my_join_info = [join_info for join_info in join_infos if join_info.user_id == self.user.id][0]
            self.user.join_infos.append(my_join_info)
            self.factory.channels.setdefault(my_join_info.channel, []).append(self)

            other_user_ids = [join_info.user_id for join_info in join_infos if join_info.user_id != self.user.id]
            self.factory.redis_session.publish(
                'create_channel',
                bson.dumps(dict(channel=channel.name, users=other_user_ids))
            )
            return channel

        def send_channel(channel, partner_ids):
            response = dict(method=u'create', channel=unicode(channel.name))
            if channel.is_group_chat:
                response['partner_ids'] = partner_ids
            else:
                response['partner_id'] = partner_ids[0]

            self.transport.write(bson.dumps(response))

        main()

    @auth_required
    def do_get_channels(self, request):

        def get_recent_messages(channel):
            return [
                dict(
                    message=message.message,
                    writer=message.writer,
                    type=message.type,
                    published_at=message.published_at
                )
                for message in DnaMessage.query(channel__eq=channel, scan_index_forward=False, limit=20)
            ]

        def get_join_infos(channel):
            return [
                dict(
                    user=join_info.user_id,
                    joined_at=join_info.joined_at,
                    last_read_at=join_info.last_read_at
                )
                for join_info in ChannelJoinInfo.by_channel(channel)
                if join_info.user_id != self.user.id
            ]

        channel_dicts = []
        channels = dict(
            (channel.name, channel)
            for channel in Channel.batch_get(*[(join_info.channel,) for join_info in self.user.join_infos])
        )
        users = set()
        for join_info in self.user.join_infos:
            channel = channels[join_info.channel]
            recent_messages = get_recent_messages(channel.name)
            if not recent_messages and not channel.is_group_chat:
                continue
            partner_join_info_dicts = get_join_infos(channel.name)
            channel_dicts.append(dict(
                channel=join_info.channel,
                unread_count=DnaMessage.query(
                    channel__eq=channel.name,
                    published_at__gt=join_info.last_read_at
                ).count(),
                recent_messages=recent_messages,
                join_infos=partner_join_info_dicts,
                is_group_chat=channel.is_group_chat
            ))
            [users.add(partner_join_info_dict['user']) for partner_join_info_dict in partner_join_info_dicts]

            last_sent_at = time.time()
            join_info.last_sent_at = last_sent_at
            join_info.save()

        response = dict(
            method=u'get_channels',
            users=list(users),
            channels=channel_dicts
        )
        self.transport.write(bson.dumps(response))

    @auth_required
    def do_unread(self, request):
        def main():
            join_infos = self.user.join_infos
            if 'channel' in request:
                join_infos = [
                    join_info
                    for join_info in join_infos
                    if join_info.channel == request['channel']
                ]
                if not join_infos:
                    raise ProtocolError('Not a valid channel')
            deferToThread(send_messages, join_infos, request.get('before'))

        def send_messages(join_infos, before=None):
            messages = []
            updated_join_infos = []
            for join_info in join_infos:
                if before:
                    new_messages = messages_before(join_info.channel, before)
                else:
                    new_messages = messages_after(join_info.channel, join_info.last_sent_at)

                if new_messages:
                    updated_join_infos.append(join_info)
                    messages += new_messages

            self.transport.write(bson.dumps(dict(method=u'unread', messages=messages)))

            for join_info in updated_join_infos:
                join_info.last_sent_at = time.time()
                join_info.save()

        def messages_before(channel, before):
            return [
                message.to_dict()
                for message in DnaMessage.query(
                    channel__eq=channel,
                    published_at__lte=before,
                    scan_index_forward=False,
                    limit=100
                )
            ]

        def messages_after(channel, after):
            return [
                message.to_dict()
                for message in DnaMessage.query(
                    channel__eq=channel,
                    published_at__gt=after
                )
            ]

        main()

    @auth_required
    def do_join(self, request):
        try:
            channel = Channel.get_item(request['channel'])
        except ItemNotFoundException:
            raise ProtocolError('Not exist channel: "%s"' % request['channel'])
        if not channel.is_group_chat:
            raise ProtocolError('Not a group chat: "%s"' % request['channel'])
        partner_ids = [join_info.user_id for join_info in ChannelJoinInfo.by_channel(channel.name)]
        self.publish_message('join', channel.name, '', self.user.id)
        ChannelJoinInfo.put_item(
            channel=channel.name,
            user_id=self.user.id,
        )
        self.transport.write(bson.dumps(dict(
            method='join',
            channel=channel.name,
            partner_ids=partner_ids
        )))

    @auth_required
    def do_withdrawal(self, request):
        def get_join_info(channel_name):
            try:
                channel = Channel.get_item(channel_name)
            except ItemNotFoundException:
                raise ProtocolError('Not exist channel: "%s"' % channel_name)
            if not channel.is_group_chat:
                raise ProtocolError('Not a group chat: "%s"' % channel_name)
            try:
                join_info = ChannelJoinInfo.get_item(channel.name, self.user.id)
            except ItemNotFoundException:
                self.transport.write(bson.dumps(dict(method=u'withdrawal', channel=channel_name)))
                return None
            return join_info

        def withdrawal(join_info):
            if not join_info:
                return
            ChannelWithdrawalLog.put_item(
                channel=join_info.channel,
                user_id=join_info.user_id,
                joined_at=join_info.joined_at,
                last_read_at=join_info.last_read_at,
            )
            join_info.delete()
            self.transport.write(bson.dumps(dict(method=u'withdrawal', channel=join_info.channel)))
            self.publish_message(u'withdrawal', join_info.channel, '', self.user.id)

        d = deferToThread(get_join_info, request['channel'])
        d.addCallback(withdrawal)

    @auth_required
    def do_attend(self, request):
        def check_is_able_to_attend(channel_name):
            for join_info in ChannelJoinInfo.by_channel(channel_name):
                if join_info.user_id == self.user.id:
                    return join_info
            else:
                raise ProtocolError('Channel is not exists')

        def attend_channel(join_info):
            self.attended_channel_join_info = join_info

            others_join_infos = [
                channel_
                for channel_ in ChannelJoinInfo.by_channel(join_info.channel)
                if channel_.user_id != self.user.id
            ]

            response = dict(method=request.method, channel=join_info.channel)
            if Channel.get_item(self.attended_channel_join_info.channel).is_group_chat:
                response['last_read'] = dict(
                    (join_info.user_id, join_info.last_read_at)
                    for join_info in others_join_infos
                )
            else:
                response['last_read'] = others_join_infos[0].last_read_at

            self.transport.write(bson.dumps(response))

        d = deferToThread(check_is_able_to_attend, request['channel'])
        d.addCallback(attend_channel)

    @in_channel_required
    def do_exit(self, request):
        self.exit_channel()

    @in_channel_required
    def do_publish(self, request):
        self.ensure_valid_message(request)
        self.attended_channel_join_info.last_sent_at = time.time()
        self.publish_message(
            request['type'],
            self.attended_channel_join_info.channel,
            request['message'],
            self.user.id
        )

    @auth_required
    def do_ack(self, request):
        message = dict(
            sender=self.user.id,
            published_at=request['published_at'],
            method=u'ack',
            channel=request['channel']
        )
        self.factory.redis_session.publish(request['channel'], bson.dumps(message))
        self.factory.log_queue.write(QueueMessage(body=json.dumps(message)))

    def do_ping(self, request):
        self.transport.write(bson.dumps(dict(method=u'ping', time=time.time())))

    def publish_message(self, type_, channel_name, message, writer):

        def write_to_sqs(message_):
            queue_message = QueueMessage(body=json.dumps(message_))
            self.factory.notification_queue.write(queue_message)
            self.factory.log_queue.write(queue_message)

        message = dict(
            type=unicode(type_),
            channel=unicode(channel_name),
            message=unicode(message),
            writer=writer,
            published_at=time.time(),
            method=u'publish',
        )
        self.factory.redis_session.publish(channel_name, bson.dumps(message))
        if self.attended_channel_join_info:
            self.attended_channel_join_info.last_published_at = message['published_at']
        deferToThread(write_to_sqs, message)

    def exit_channel(self):
        if not self.user:
            return
        if not self.attended_channel_join_info:
            return

        if hasattr(self.attended_channel_join_info, 'last_published_at'):
            published_at = self.attended_channel_join_info.last_published_at
            delattr(self.attended_channel_join_info, 'last_published_at')
            ChannelUsageLog.put_item(
                date=datetime.datetime.fromtimestamp(published_at).strftime('%Y-%m-%d'),
                channel=self.attended_channel_join_info.channel,
                last_published_at=published_at
            )
        self.attended_channel_join_info = None

    def connectionLost(self, reason=None):
        logger.info('Connection Lost : %s' % reason)
        if not self.user:
            return
        self.exit_channel()
        for join_info in self.user.join_infos:
            self.factory.channels[join_info.channel].remove(self)

    def authenticate(self, request):
        """
        Authenticate this connection and return a User
        :param request: dnachat.dna.request.Request object
        :return: A user object that has property "id". If failed, returns None
        """
        raise NotImplementedError

    def ensure_valid_message(self, request):
        if not request['message'].strip():
            raise ProtocolError('Blank message is not accepted')

    @staticmethod
    def get_user_by_id(user_id):
        """
        Return a user by user_id
        :param user_id: id of user
        :return: A user object
        """
        raise NotImplementedError


class ChatFactory(Factory):

    def __init__(self, redis_host='localhost'):
        self.protocol = conf['PROTOCOL']
        self.channels = dict()
        self.redis_session = redis.StrictRedis(host=redis_host)
        sqs_conn = sqs.connect_to_region('ap-northeast-1')
        self.notification_queue = sqs_conn.get_queue(conf['NOTIFICATION_QUEUE_NAME'])
        self.log_queue = sqs_conn.get_queue(conf['LOG_QUEUE_NAME'])
        Transmitter(self).start()

