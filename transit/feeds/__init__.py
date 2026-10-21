"""Registry of active feed adapters. Add a new source by appending an adapter here."""

from .. import config
from ..njt import NjtClient
from .base import FeedAdapter, GtfsRtAdapter
from .njt_bus import NjtBusAdapter
from .njt_rail import NjtRailAdapter

_njt = NjtClient()  # one token store shared by rail and bus

ADAPTERS: list[FeedAdapter] = [
    # LIRR publishes every ~3s but positions are often minutes old; 10s keeps Vercel usage in the free tier.
    GtfsRtAdapter(agency="lirr", url=config.AGENCIES["lirr"].realtime_url, cache_s=10),
    GtfsRtAdapter(agency="mnr", url=config.AGENCIES["mnr"].realtime_url, cache_s=10),  # publishes ~every 11s
    NjtRailAdapter(njt=_njt),
    NjtBusAdapter(agency="njt_bus", njt=_njt),
]
