# -*-coding:utf8-*-
from threading import Thread
import bson


class Transmitter(Thread):
    def __init__(self, factory):
        Thread.__init__(self)
        self.factory = factory

    def run(self):
        pubsub = self.factory.redis_session.pubsub()
        pubsub.psubscribe('*')
        pubsub.listen().next()
        for message in pubsub.listen():
            data = bson.loads(message['data'])
            for client in self.factory.channels[message['channel']]:
                client.transport.write(message['data'])
                client.channel.last_read_at = data['published_at']
