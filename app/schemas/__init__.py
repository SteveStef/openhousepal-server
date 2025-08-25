from .user import User, UserCreate, UserUpdate, UserLogin, Token, TokenData
from .collection import Collection, CollectionCreate, CollectionUpdate, CollectionWithProperties, ShareCollectionRequest, ShareCollectionResponse
from .property import Property, PropertyCreate, PropertyUpdate, PropertySummary, AddPropertyToCollection

# Resolve forward references
CollectionWithProperties.model_rebuild()