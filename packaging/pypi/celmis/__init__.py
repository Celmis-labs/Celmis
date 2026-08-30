"""Offline verifier for a Celmis evidence pack. Not the Celmis platform.

The platform lives at https://github.com/Celmis-labs/Celmis and runs under
docker compose; this is the few hundred lines of standard library an auditor
needs to check one of its evidence packs without installing it, or trusting it.
"""

from celmis.verify import MANIFEST_VERSION

#: Versioned independently of the platform, because this changes only when the
#: pack format does. Tying them would force a release here for every platform
#: patch and imply a compatibility relationship that does not exist.
__version__ = "0.1.0"

__all__ = ["MANIFEST_VERSION", "__version__"]
