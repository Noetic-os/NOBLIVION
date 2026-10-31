# SPDX-License-Identifier: AGPL-3.0-or-later
"""The long-run test program: NOBLIVION against stock memory, with no model call.

The plan is in the evaluation doc. The modules:

* ``config`` - frozen parameters, the decision rules, the pre-registration text
* ``sandbox`` - the checks that keep the run at zero tokens and read-only
* ``fixtures`` - the scale fixture of ``tools/eval/scale_fixture.py``, loaded
* ``stocksim`` - stock memory without a model: S0, S1, S2, S-cal
* ``buildrunner`` - one build on a harness store, its hooks run in a worker
* ``replay`` - arms b and c: the claims C1 to C24
* ``live`` - arm a: counts from the transcripts and the plugin logs
* ``soak`` - arm d: load, churn, drift, lag and the latency benchmark
* ``judge`` - arm e: the local model, over its HTTP API only
* ``labels`` - the labelled dev split, through its loader
* ``stats`` - Wilson, McNemar, bootstrap, Holm, Theil-Sen
* ``report`` - the daily summary, the final report and the sanitised export
* ``scheduler`` - the internal schedule and its state file
* ``cli`` - the commands

Run it as ``python -m longtest`` with
``tools`` on the path.
"""
