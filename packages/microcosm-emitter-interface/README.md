# Microcosm emitter interface

`microcosm-emitter-interface` is the small, dependency-free package a Microcosm build imports to hand work to the separately installed `microcosm-emitter`. Microcosm decides what to hand over; the emitter decides where it goes.

Install the emitter on its own, outside the build's environment:

```bash
uv tool install microcosm-emitter
```

The build then depends on this package with a version range and starts the emitter through it:

```python
from microcosm_emitter_interface import EmitterClient, TelemetryRun, TelemetrySettings

client = EmitterClient.start(
    telemetry=TelemetrySettings(run=TelemetryRun("run-id", "UK", "uk-build")),
)
client.telemetry.transition_stage("calibrating")
client.telemetry.complete()
client.close()
```

`EmitterClient.start` runs the `microcosm-emitter` command as the build's own child process. The command comes from the `command` argument, then the `MICROCOSM_EMITTER_COMMAND` environment variable, then `PATH`. The child inherits the build's environment variables and user, so it finds the same Hugging Face credential: `HF_TOKEN`, then `HUGGINGFACE_TOKEN`, then the token saved by `hf auth login`.

No call raises because the emitter is missing, slow or refusing a request. Each call reports whether the emitter accepted it, and the build continues.
