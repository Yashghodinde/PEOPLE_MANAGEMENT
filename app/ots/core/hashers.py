from django.contrib.auth.hashers import Argon2PasswordHasher


class OtsArgon2Hasher(Argon2PasswordHasher):
    """Argon2id with the parameters fixed by the architecture contract."""

    time_cost = 2
    memory_cost = 19456  # KiB
    parallelism = 1
