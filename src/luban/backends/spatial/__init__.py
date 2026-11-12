"""In-process spatial inference backends and output validation."""

from luban.backends.spatial.schema import validate_spatial_context
from luban.backends.spatial.spatiallm import SpatialLMBackend

__all__ = ["SpatialLMBackend", "validate_spatial_context"]
