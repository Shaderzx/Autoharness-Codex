"""Bridge Codex's file/direct OS keyring auth into a private learner home.

Storage contract: openai/codex rust-v0.160.1, login/src/auth/storage.rs.
No credential values are passed as process arguments or included in errors.
"""
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import os
import sys
import threading
import tomllib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from codex_autoharness.lib import atomic

_SERVICE = b"Codex Auth"
_LIMIT = 1_000_000


class AuthError(ValueError):
    """Sanitized credential-storage failure."""


def _store_key(source):
    """Derive Codex's home-specific direct keyring account."""
    return "cli|" + hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:16]


def _mac_keyring(account, value=None):
    """Read or update an existing item in the default macOS user keychain."""
    libraries = [ctypes.util.find_library(name) for name in ("Security", "CoreFoundation")]
    if not all(libraries):
        raise AuthError("auth_keyring_unavailable")
    security, core = (ctypes.CDLL(name) for name in libraries)
    pointer = ctypes.c_void_p
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_ubyte]
    security.SecKeychainCopyDomainDefault.argtypes = [ctypes.c_int, ctypes.POINTER(pointer)]
    security.SecKeychainFindGenericPassword.argtypes = [pointer, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.c_uint32, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, ctypes.c_uint32, ctypes.c_char_p]
    security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    core.CFRelease.argtypes = [pointer]
    keychain, item, data, length = pointer(), pointer(), pointer(), ctypes.c_uint32()
    try:
        if security.SecKeychainSetUserInteractionAllowed(False):
            raise AuthError("auth_keyring_unavailable")
        if security.SecKeychainCopyDomainDefault(0, ctypes.byref(keychain)):
            raise AuthError("auth_keyring_unavailable")
        user = account.encode("utf-8")
        status = security.SecKeychainFindGenericPassword(keychain, len(_SERVICE), _SERVICE,
            len(user), user, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        if status == -25300:  # errSecItemNotFound; never recreate a deleted login.
            if value is not None:
                raise AuthError("auth_source_changed")
            return None
        if status or length.value > _LIMIT:
            raise AuthError("auth_keyring_unavailable")
        if value is None:
            return ctypes.string_at(data, length.value)
        if security.SecKeychainItemModifyAttributesAndData(item, None, len(value), value):
            raise AuthError("auth_refresh_save_failed")
    finally:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        if item:
            core.CFRelease(item)
        if keychain:
            core.CFRelease(keychain)


class _GList(ctypes.Structure):
    """GLib list-node layout retaining each matched Secret Service item."""
    _fields_ = [("data", ctypes.c_void_p), ("next", ctypes.c_void_p), ("previous", ctypes.c_void_p)]


def _linux_keyring(account, value=None):
    """Read or update the unique Secret Service item in its own collection."""
    # Match keyring 3.6.3 and update the exact item, even outside the current
    # default collection. Creating an item could duplicate a moved login.
    libraries = [ctypes.util.find_library(name) for name in ("secret-1", "glib-2.0", "gobject-2.0", "gio-2.0")]
    if not all(libraries):
        raise AuthError("auth_keyring_unavailable")
    secret, glib, objects, gio = (ctypes.CDLL(name) for name in libraries)
    pointer = ctypes.c_void_p
    error_pointer = ctypes.POINTER(pointer)
    signatures = {
        "secret_service_get_sync": (pointer, [ctypes.c_uint, pointer, error_pointer]),
        "secret_service_search_sync": (pointer, [pointer, pointer, pointer, ctypes.c_uint, pointer, error_pointer]),
        "secret_collection_for_alias_sync": (pointer, [pointer, ctypes.c_char_p, ctypes.c_uint, pointer, error_pointer]),
        "secret_collection_search_sync": (pointer, [pointer, pointer, pointer, ctypes.c_uint, pointer, error_pointer]),
        "secret_item_get_secret": (pointer, [pointer]),
        "secret_value_get": (pointer, [pointer, ctypes.POINTER(ctypes.c_size_t)]),
        "secret_value_new": (pointer, [ctypes.c_char_p, ctypes.c_ssize_t, ctypes.c_char_p]),
        "secret_item_set_secret_sync": (ctypes.c_int, [pointer, pointer, pointer, error_pointer]),
        "secret_value_unref": (None, [pointer]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(secret, name)
        function.restype, function.argtypes = result, arguments
    glib.g_hash_table_new.restype, glib.g_hash_table_new.argtypes = pointer, [pointer, pointer]
    glib.g_hash_table_insert.argtypes = [pointer, ctypes.c_char_p, ctypes.c_char_p]
    glib.g_hash_table_remove.argtypes = [pointer, ctypes.c_char_p]
    for name in ("g_hash_table_unref", "g_list_free", "g_error_free"):
        getattr(glib, name).argtypes = [pointer]
    objects.g_object_unref.argtypes = [pointer]
    gio.g_cancellable_new.restype = pointer
    gio.g_cancellable_cancel.argtypes = [pointer]
    cancellable = gio.g_cancellable_new()
    timeout = threading.Timer(15, gio.g_cancellable_cancel, args=(cancellable,))
    timeout.start()
    attributes = [(b"target", b"default"), (b"service", _SERVICE), (b"username", account.encode())]
    table = glib.g_hash_table_new(ctypes.cast(glib.g_str_hash, pointer), ctypes.cast(glib.g_str_equal, pointer))
    service = collection = items = stored = None
    error = pointer()
    try:
        for key, attribute in attributes:
            glib.g_hash_table_insert(table, key, attribute)
        service = secret.secret_service_get_sync(2, cancellable, ctypes.byref(error))
        if error or not service:
            raise AuthError("auth_keyring_unavailable")
        # SECRET_SEARCH_ALL | SECRET_SEARCH_UNLOCK | SECRET_SEARCH_LOAD_SECRETS
        items = secret.secret_service_search_sync(service, None, table, 14, cancellable, ctypes.byref(error))
        if error:
            raise AuthError("auth_keyring_unavailable")
        if not items:
            # Rust keyring also accepts legacy entries without a target,
            # but only from the default collection.
            glib.g_hash_table_remove(table, b"target")
            collection = secret.secret_collection_for_alias_sync(service, b"default", 0, cancellable, ctypes.byref(error))
            if error:
                raise AuthError("auth_keyring_unavailable")
            if collection:
                items = secret.secret_collection_search_sync(collection, None, table, 14, cancellable, ctypes.byref(error))
            if error:
                raise AuthError("auth_keyring_unavailable")
        if not items:
            if value is not None:
                raise AuthError("auth_source_changed")
            return None
        item = ctypes.cast(items, ctypes.POINTER(_GList)).contents
        if item.next:
            raise AuthError("auth_keyring_ambiguous")
        if value is None:
            stored = secret.secret_item_get_secret(item.data)
            if not stored:
                raise AuthError("auth_keyring_unavailable")
            length = ctypes.c_size_t()
            data = secret.secret_value_get(stored, ctypes.byref(length))
            if not data or length.value > _LIMIT:
                raise AuthError("auth_invalid")
            return ctypes.string_at(data, length.value)
        stored = secret.secret_value_new(value, len(value), b"text/plain")
        if not stored or not secret.secret_item_set_secret_sync(item.data, stored, cancellable, ctypes.byref(error)) or error:
            raise AuthError("auth_refresh_save_failed")
    finally:
        timeout.cancel()
        timeout.join()
        objects.g_object_unref(cancellable)
        if stored:
            secret.secret_value_unref(stored)
        current = items
        while current:
            node = ctypes.cast(current, ctypes.POINTER(_GList)).contents
            objects.g_object_unref(node.data)
            current = node.next
        if items:
            glib.g_list_free(items)
        for object_pointer in (collection, service):
            if object_pointer:
                objects.g_object_unref(object_pointer)
        glib.g_hash_table_unref(table)
        if error:
            glib.g_error_free(error)


def _keyring(source, value=None):
    """Route direct keyring access to the native platform backend."""
    try:
        if sys.platform == "darwin":
            return _mac_keyring(_store_key(source), value)
        if sys.platform.startswith("linux"):
            return _linux_keyring(_store_key(source), value)
        raise AuthError("auth_keyring_unavailable")
    except (OSError, AttributeError, ctypes.ArgumentError) as exc:
        raise AuthError("auth_keyring_unavailable") from exc


def _file(path):
    """Read bounded authentication bytes without following a symlink."""
    if path.is_symlink():
        raise AuthError("auth_symlink")
    if not path.exists():
        return None
    if path.stat().st_size > _LIMIT:
        raise AuthError("auth_invalid")
    return path.read_bytes()


def _load(source, mode, encrypted):
    """Honor Codex's credential mode and keyring-first auto fallback."""
    if mode not in {"file", "keyring", "auto", "ephemeral"}:
        raise AuthError("auth_store_invalid")
    if mode == "ephemeral":
        return None, None  # In-process credentials cannot cross a process boundary.
    if mode in {"keyring", "auto"}:
        if encrypted:
            raise AuthError("auth_encrypted_keyring_unsupported")
        try:
            value = _keyring(source)
            if value is not None:
                _decode(value)
                return value, "keyring"
        except AuthError:
            if mode == "keyring":
                raise
        if mode == "keyring":
            return None, "keyring"
    return _file(source / "auth.json"), "file"


def _decode(value):
    """Validate stored auth shape without including credential values in errors."""
    if value is None:
        return None
    try:
        data = json.loads(value)
        if not isinstance(data, dict):
            raise ValueError
        for key in ("auth_mode", "OPENAI_API_KEY", "last_refresh"):
            if data.get(key) is not None and not isinstance(data[key], str):
                raise ValueError
        if data.get("auth_mode") not in {None, "apikey", "chatgpt", "chatgptAuthTokens", "headers",
                                          "agentIdentity", "personalAccessToken", "bedrockApiKey", "bedrockAccessKeys"}:
            raise ValueError
        if data.get("last_refresh") is not None:
            timestamp = data["last_refresh"]
            if "T" not in timestamp.upper() or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is None:
                raise ValueError
        tokens = data.get("tokens")
        if tokens is not None and (not isinstance(tokens, dict) or any(
                not isinstance(tokens.get(key), str) for key in ("id_token", "access_token", "refresh_token"))):
            raise ValueError
        if tokens and tokens.get("account_id") is not None and not isinstance(tokens["account_id"], str):
            raise ValueError
        return data
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AuthError("auth_invalid") from exc


def _refreshed(before, after):
    """Accept only newer, complete OAuth tokens for the original account."""
    old, new = before.get("tokens"), after.get("tokens")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if not isinstance(old.get("account_id"), str) or not old["account_id"] or old["account_id"] != new.get("account_id"):
        return False
    if before.get("auth_mode") != after.get("auth_mode"):
        return False
    if any(not isinstance(new.get(key), str) or not new[key] for key in ("id_token", "access_token", "refresh_token")):
        return False
    try:
        refreshed = datetime.fromisoformat(after["last_refresh"].replace("Z", "+00:00"))
        previous = datetime.fromisoformat(before["last_refresh"].replace("Z", "+00:00")) if before.get("last_refresh") else None
        return refreshed.utcoffset() is not None and (previous is None or refreshed > previous)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


@contextmanager
def isolated_credentials(source, target):
    """Copy private auth and persist valid refreshes under a learner lock."""
    source, target = Path(source).expanduser().resolve(), Path(target)
    mode, encrypted = _settings(source)
    initial, backend = _load(source, mode, encrypted)
    before = _decode(initial)
    descriptor = None
    try:
        if before and isinstance(before.get("tokens"), dict):
            # ponytail: serialize OAuth learners per home; foreground Codex uses
            # its own locks, so compare its current login again before saving.
            descriptor = os.open(source / ".autoharness-auth.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            mode, encrypted = _settings(source)
            initial, backend = _load(source, mode, encrypted)
            before = _decode(initial)
        if initial is not None:
            atomic.write_bytes(target / "auth.json", initial)
        else:
            (target / "auth.json").unlink(missing_ok=True)
        try:
            yield
        finally:
            after = _decode(_file(target / "auth.json"))
            if before and after and before != after and _refreshed(before, after):
                if _settings(source) == (mode, encrypted):
                    current, current_backend = _load(source, mode, encrypted)
                    if current_backend == backend and _decode(current) == before:
                        tokens = {**before["tokens"], **{key: after["tokens"][key] for key in
                                                       ("id_token", "access_token", "refresh_token")}}
                        updated = {**before, "tokens": tokens, "last_refresh": after["last_refresh"]}
                        value = json.dumps(updated).encode("utf-8")
                        if backend == "keyring":
                            _keyring(source, value)
                        else:
                            atomic.write_bytes(source / "auth.json", value)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _settings(source):
    """Read the active profile's effective credential-storage settings."""
    config_path = source / "config.toml"
    data = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    profile, profiles, features = data.get("profile"), data.get("profiles", {}), data.get("features", {})
    if (profile is not None and not isinstance(profile, str)) or not isinstance(profiles, dict):
        raise AuthError("auth_store_invalid")
    if profile:
        selected = profiles.get(profile, {})
        if not isinstance(selected, dict):
            raise AuthError("auth_store_invalid")
        profile_features = selected.get("features", {})
        if not isinstance(features, dict) or not isinstance(profile_features, dict):
            raise AuthError("auth_store_invalid")
        data = {**data, **selected}
        features = {**features, **profile_features}
    if not isinstance(features, dict):
        raise AuthError("auth_store_invalid")
    mode = data.get("cli_auth_credentials_store", "file")
    encrypted = features.get("secret_auth_storage", False)
    if not isinstance(mode, str) or not isinstance(encrypted, bool):
        raise AuthError("auth_store_invalid")
    return mode, encrypted
