# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""aiseed_weather package — third-party warning filters live here.

The filters in this file run on every import of the package (CLI
entry point, tests, ad-hoc scripts) so deprecation noise that comes
from libraries we depend on but don't control is silenced uniformly.
"""

import warnings

# cfgrib calls ``xr.merge`` inside ``cfgrib.open_datasets`` when
# stitching GRIB hypercubes together. xarray ≥ 2026 is preparing to
# flip the ``compat`` default from 'no_conflicts' to 'override' and
# emits a FutureWarning on every merge until callers opt in. That
# fires once per (run, step) decode and floods the log with several
# identical lines. The warning is a notice about a planned API
# change in a transitive dependency, not something this code can
# influence — suppress it here so the rest of our log stays
# readable.
warnings.filterwarnings(
    "ignore",
    category=FutureWarning,
    module=r"cfgrib\.xarray_store",
)
