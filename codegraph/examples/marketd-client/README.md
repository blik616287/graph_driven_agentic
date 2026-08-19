# marketd-client (demo fixture)

A deliberately small, deliberately *related* second codebase: a client SDK that
mirrors part of `example_src/marketd`'s domain and API surface.

It exists to exercise cross-root synthetic linking. The two codebases share no
imports — nothing here imports marketd — so the only way a graph can connect
them is by inference:

- **shared_symbol** — both define `Instrument`, `OrderStatus`, `TimeInForce`,
  `place_order`
- **mirrored_path** — both have `marketd*/core/models.py`

That is the situation synthetic edges are for: a client and its server, a spec
and its implementation, a service and its generated bindings.
