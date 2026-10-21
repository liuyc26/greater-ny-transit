"""Registry of active feed adapters. Add a new source by appending an adapter here."""

from .. import config
from ..kv import kv_from_env
from ..njt import NjtClient
from ..snapshot import SharedSnapshot, SnapshotAdapter
from .base import FeedAdapter, GtfsRtAdapter
from .njt_bus import NjtBusAdapter
from .njt_rail import NjtRailAdapter

NJT = NjtClient()  # one token store shared by rail and bus

# NJ Transit has daily request limits, so visitors never trigger NJT calls directly: rail and bus are
# fetched together into one shared snapshot at most every 30s for the whole site (transit/snapshot.py).
NJT_SNAPSHOT = SharedSnapshot(
    key="snapshot:njt",
    adapters=[NjtRailAdapter(njt=NJT), NjtBusAdapter(agency="njt_bus", njt=NJT)],
    ttl_s=30,
    kv=kv_from_env(),
)

ADAPTERS: list[FeedAdapter] = [
    # MTA feeds have no stated limit: served via the CDN cache only.
    # LIRR publishes every ~3s but positions are often minutes old; 10s keeps Vercel usage in the free tier.
    GtfsRtAdapter(agency="lirr", url=config.AGENCIES["lirr"].realtime_url, cache_s=10),
    GtfsRtAdapter(agency="mnr", url=config.AGENCIES["mnr"].realtime_url, cache_s=10),  # publishes ~every 11s
    SnapshotAdapter(NJT_SNAPSHOT, "njt_rail", cache_s=10),
    SnapshotAdapter(NJT_SNAPSHOT, "njt_bus", cache_s=10),
]
