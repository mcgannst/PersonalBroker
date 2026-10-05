"""Option strategy plug-ins (OPTSIM): the framework (`base`, and from T7 `registry`, `state`, `host`) and the
plug-ins themselves, one package each. A plug-in touches only its own package, its own tables and
`option_strategy_state`; the core never imports a plug-in and never names one."""
