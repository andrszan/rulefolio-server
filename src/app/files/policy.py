from datetime import timedelta

ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGES_PER_WORK = 12
MAX_IMAGE_BYTES_PER_WORK = 30 * 1024 * 1024
PENDING_IMAGE_TTL = timedelta(minutes=2)
S3_TIMEOUT_SECONDS = 10
UPLOAD_CHUNK_SIZE = 64 * 1024
