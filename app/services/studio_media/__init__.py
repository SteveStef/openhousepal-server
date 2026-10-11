"""Studio AI Property Media - AutoReel-style photo/video tools inside Studio.

Public surface lives in app/services/studio_media_service.py. This package holds the
model-provider seam (providers/) and the prompt builders (prompts.py) so a different
image/video model can be swapped in by config without touching the service or routes.
"""
