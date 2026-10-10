# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import sys

UPGRADING_GUIDE_URL = "https://AustralianCancerDataNetwork.github.io/oa-configurator/upgrading-from-1.x/"
TRACKING_ISSUE_URL = "https://github.com/AustralianCancerDataNetwork/oa-configurator/issues/40"


if __name__ == "__main__":
    print("The 1.x to 2.0 migration script is not available yet.", file=sys.stderr)
    print(f"Follow the manual upgrade steps at {UPGRADING_GUIDE_URL}.", file=sys.stderr)
    print(f"Tracking issue: {TRACKING_ISSUE_URL}", file=sys.stderr)
    raise SystemExit(2)
