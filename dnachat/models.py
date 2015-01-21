from bynamodb.attributes import StringAttribute, NumberAttribute
from bynamodb.indexes import GlobalAllIndex
from bynamodb.model import Model

from .settings import conf


class Joiner(Model):
    table_name = '%sJoiner' % conf.get('prefix', '')

    key = StringAttribute(hash_key=True)
    channel = StringAttribute()
    user_id = StringAttribute()

    class UserIndex(GlobalAllIndex):
        hash_key = 'user_id'

        read_throughput = 1
        write_throughput = 1

    class ChannelIndex(GlobalAllIndex):
        hash_key = 'channel'

        read_throughput = 1
        write_throughput = 1


class Message(Model):
    table_name = '%sMessage' % conf.get('prefix', '')

    channel = StringAttribute(hash_key=True)
    published_at = NumberAttribute(range_key=True)
    user = StringAttribute()
    message = StringAttribute()

    def to_dict(self):
        return dict(writer=self.user, published_at=float(self.published_at), message=self.message, channel=self.channel)
