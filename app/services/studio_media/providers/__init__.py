"""Model providers for Studio AI Property Media.

Only OpenAI (gpt-image-2, image editing) and Runway (gen4.5, image-to-video) are implemented
today. `registry.py` resolves a provider by the name in ai_config_service DEFAULT_CONFIG, so
adding e.g. Veo or FLUX later is a new module + a registry entry, nothing else.
"""
