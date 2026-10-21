"""The Hugging Face hand-over module; constructed only by the service host."""

from microcosm_emitter.host.contracts import JsonObject, ModuleContext, ServiceModule


def create_module(configuration: JsonObject, context: ModuleContext) -> ServiceModule:
    """Construct the module without starting workers or performing network I/O."""
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    return HuggingFaceModule(configuration, context)
