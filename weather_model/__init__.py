"""
weather_model — pure-function forecast → bucket-probability → edge pipeline.

This package is a curated extract from a larger research project investigating
whether retail edge exists in Polymarket prediction markets.  It demonstrates:

  - Gaussian-CDF bucket probability model (stdlib math.erf only)
  - Multi-model forecast blending with an anti-overfit safety guarantee
  - Fractional-Kelly position sizing and edge gatekeeping
  - City/station registry (ICAO coords for resolution-station accuracy)
  - METAR/ASOS realized-high client (two independent public sources)

No execution code, no live trading, no secrets.  stdlib + requests only.
Run the unit tests with:  pytest weather_model/tests
"""
