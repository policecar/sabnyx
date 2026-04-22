"""Interface normalizers (NormalizedEnvelope pattern).

Every interface (cli, telegram, email, web, slack, ...) writes a thin
normalizer that maps its raw events to NormalizedEnvelope. Downstream
code never sees interface-specific payloads.

Stage 0 implements only `cli`. Adding a new interface = one new file
that maps events to NormalizedEnvelope.
"""
