import ctypes
import hashlib
import json
import subprocess
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autoharness.hook import auth, spawn


def credential(refresh="old", when="2026-01-01T00:00:00Z", account="account"):
    """Build a synthetic managed OAuth credential with preserved metadata."""
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": "fake-id", "access_token": "fake-access",
                       "refresh_token": refresh, "account_id": account}, "last_refresh": when,
            "future_metadata": {"keep": True}}


@pytest.fixture
def homes(tmp_path):
    """Create independent source and learner homes for credential checks."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    return source, target


def write(path, value):
    """Write only synthetic credential JSON into a fixture home."""
    path.write_text(json.dumps(value))


def test_keyring_identity_uses_canonical_codex_home(homes, tmp_path):
    """Keyring identity uses canonical codex home."""
    source, _ = homes
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    expected = "cli|" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16]
    assert auth._store_key(alias) == auth._store_key(source) == expected


def test_keyring_oauth_refresh_returns_to_original_store(homes, monkeypatch):
    """Keyring oauth refresh returns to original store."""
    source, target = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "auto"\n')
    write(source / "auth.json", {"OPENAI_API_KEY": "stale-fallback"})
    stored = json.dumps(credential()).encode()
    def keyring(home, value=None):
        """Model reads and in-place writes to the original keyring login."""
        nonlocal stored
        assert home == source
        if value is not None:
            stored = value
        return stored
    monkeypatch.setattr(auth, "_keyring", keyring)
    with auth.isolated_credentials(source, target):
        assert json.loads((target / "auth.json").read_bytes()) == credential()
        assert (target / "auth.json").stat().st_mode & 0o777 == 0o600
        write(target / "auth.json", credential("new", "2026-01-02T00:00:00Z"))
    assert json.loads(stored) == credential("new", "2026-01-02T00:00:00Z")
    assert json.loads((source / "auth.json").read_bytes()) == {"OPENAI_API_KEY": "stale-fallback"}


@pytest.mark.parametrize("change", ["refresh", "logout", "relogin", "config"])
def test_foreground_auth_changes_prevent_stale_writeback(homes, change):
    """Foreground auth changes prevent stale writeback."""
    source, target = homes
    path = source / "auth.json"
    write(path, credential())
    with auth.isolated_credentials(source, target):
        write(target / "auth.json", credential("learner", "2026-01-02T00:00:00Z"))
        if change == "logout":
            path.unlink()
        elif change == "config":
            (source / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
        else:
            write(path, credential("foreground", "2026-01-03T00:00:00Z",
                                   account="another-account" if change == "relogin" else "account"))
        current = path.read_bytes() if path.exists() else None
    assert (path.read_bytes() if path.exists() else None) == current


@pytest.mark.parametrize("mutation", [
    {"tokens": None}, {"tokens": {**credential()["tokens"], "account_id": "another"}},
    {"last_refresh": "invalid"}, {"last_refresh": "2025-01-01T00:00:00Z"},
    {"last_refresh": "2026-01-02T00:00:00"}, {"auth_mode": "apikey"},
])
def test_invalid_or_different_account_refresh_cannot_replace_source(homes, mutation):
    """Invalid or different account refresh cannot replace source."""
    source, target = homes
    write(source / "auth.json", credential())
    with auth.isolated_credentials(source, target):
        changed = {**credential("new", "2026-01-02T00:00:00Z"), **mutation}
        write(target / "auth.json", changed)
    assert json.loads((source / "auth.json").read_bytes()) == credential()


def test_refresh_preserves_unknown_source_fields(homes):
    """Refresh preserves unknown source fields."""
    source, target = homes
    write(source / "auth.json", credential())
    with auth.isolated_credentials(source, target):
        changed = credential("new", "2026-01-02T00:00:00Z")
        changed.pop("future_metadata")
        write(target / "auth.json", changed)
    assert json.loads((source / "auth.json").read_bytes()) == credential("new", "2026-01-02T00:00:00Z")


def test_unrelated_model_configuration_edit_does_not_discard_refresh(homes):
    """Unrelated model configuration edit does not discard refresh."""
    source, target = homes
    (source / "config.toml").write_text('model = "before"\n')
    write(source / "auth.json", credential())
    with auth.isolated_credentials(source, target):
        (source / "config.toml").write_text('model = "after"\n')
        write(target / "auth.json", credential("new", "2026-01-02T00:00:00Z"))
    assert json.loads((source / "auth.json").read_bytes()) == credential("new", "2026-01-02T00:00:00Z")


def test_settings_are_reloaded_after_waiting_for_learner_lock(homes, monkeypatch):
    """Settings are reloaded after waiting for learner lock."""
    source, target = homes
    write(source / "auth.json", credential())
    monkeypatch.setattr(auth.fcntl, "flock", lambda *args:
                        (source / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n'))
    with auth.isolated_credentials(source, target):
        assert not (target / "auth.json").exists()


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_spawn_persists_refresh_even_when_proposal_fails(homes, tmp_path, failure):
    """Spawn persists refresh even when proposal fails."""
    source, _ = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    write(source / "auth.json", credential())
    def child(argv, env, bundle):
        """Rotate a fixture credential before failing the proposer."""
        target = Path(env["CODEX_HOME"])
        config = tomllib.loads((target / "config.toml").read_text())
        assert config["cli_auth_credentials_store"] == "file"
        assert "hooks" not in config and "mcp_servers" not in config
        write(target / "auth.json", credential("new", "2026-01-02T00:00:00Z"))
        if failure == "timeout":
            raise subprocess.TimeoutExpired("fake-secret", 1, stderr="fake-secret")
        return SimpleNamespace(returncode=1)
    with pytest.raises(spawn.RunnerError, match="timeout|child_exit_failure"):
        spawn.run("window", "test-auth-" + failure, source_home=source,
                  roots={"project": tmp_path / "p", "global": tmp_path / "g"}, spawn_fn=child)
    assert json.loads((source / "auth.json").read_bytes()) == credential("new", "2026-01-02T00:00:00Z")
    assert (source / "auth.json").stat().st_mode & 0o777 == 0o600


def test_authenticated_learners_reload_after_lock(homes, tmp_path):
    """Authenticated learners reload after lock."""
    source, first = homes
    second = tmp_path / "second"
    second.mkdir()
    write(source / "auth.json", credential())
    started, acquired = threading.Event(), threading.Event()
    failures = []
    def worker():
        """Exercise a second learner waiting for the shared OAuth lock."""
        try:
            started.set()
            with auth.isolated_credentials(source, second):
                acquired.set()
                assert json.loads((second / "auth.json").read_bytes())["tokens"]["refresh_token"] == "first"
                write(second / "auth.json", credential("second", "2026-01-03T00:00:00Z"))
        except BaseException as exc:
            failures.append(exc)
    with auth.isolated_credentials(source, first):
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(2)
        assert not acquired.wait(0.05)
        write(first / "auth.json", credential("first", "2026-01-02T00:00:00Z"))
    thread.join(2)
    assert not thread.is_alive() and not failures
    assert json.loads((source / "auth.json").read_bytes())["tokens"]["refresh_token"] == "second"


@pytest.mark.parametrize("mode", ["auto", "keyring"])
def test_missing_keyring_helper_falls_back_only_in_auto(homes, monkeypatch, mode):
    """Missing keyring helper falls back only in auto."""
    source, target = homes
    (source / "config.toml").write_text(f'cli_auth_credentials_store = "{mode}"\n')
    write(source / "auth.json", {"OPENAI_API_KEY": "fake-key"})
    def unavailable(*args):
        """Simulate a locked or missing native credential store."""
        raise auth.AuthError("auth_keyring_unavailable")
    monkeypatch.setattr(auth, "_keyring", unavailable)
    if mode == "keyring":
        with pytest.raises(auth.AuthError, match="auth_keyring_unavailable"):
            with auth.isolated_credentials(source, target):
                pytest.fail("strict keyring mode cannot start")
    else:
        with auth.isolated_credentials(source, target):
            assert json.loads((target / "auth.json").read_bytes()) == {"OPENAI_API_KEY": "fake-key"}


def test_ephemeral_store_does_not_copy_persisted_credentials(homes):
    """Ephemeral store does not copy persisted credentials."""
    source, target = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "ephemeral"\n')
    write(source / "auth.json", credential())
    write(target / "auth.json", credential())
    with auth.isolated_credentials(source, target):
        assert not (target / "auth.json").exists()


def test_encrypted_keyring_reports_safe_unsupported_error(homes):
    """Encrypted keyring reports safe unsupported error."""
    source, target = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n[features]\nsecret_auth_storage = true\n')
    with pytest.raises(auth.AuthError, match="^auth_encrypted_keyring_unsupported$"):
        with auth.isolated_credentials(source, target):
            pytest.fail("encrypted keyring is a different storage contract")


def test_invalid_keyring_json_uses_auto_file_fallback(homes, monkeypatch):
    """Invalid keyring json uses auto file fallback."""
    source, target = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "auto"\n')
    write(source / "auth.json", {"OPENAI_API_KEY": "fake-key"})
    monkeypatch.setattr(auth, "_keyring", lambda *args: b"invalid-fake-secret")
    with auth.isolated_credentials(source, target):
        assert json.loads((target / "auth.json").read_bytes()) == {"OPENAI_API_KEY": "fake-key"}


def test_invalid_keyring_token_structure_uses_auto_file_fallback(homes, monkeypatch):
    """Invalid keyring token structure uses auto file fallback."""
    source, target = homes
    (source / "config.toml").write_text('cli_auth_credentials_store = "auto"\n')
    write(source / "auth.json", {"OPENAI_API_KEY": "fake-key"})
    monkeypatch.setattr(auth, "_keyring", lambda *args: b'{"tokens":"invalid-type"}')
    with auth.isolated_credentials(source, target):
        assert json.loads((target / "auth.json").read_bytes()) == {"OPENAI_API_KEY": "fake-key"}


@pytest.mark.parametrize("setting", [
    'cli_auth_credentials_store = ["fake-secret"]',
    'cli_auth_credentials_store = "keyring"\n[features]\nsecret_auth_storage = "fake-secret"',
])
def test_invalid_store_settings_have_sanitized_errors(homes, setting):
    """Invalid store settings have sanitized errors."""
    source, target = homes
    (source / "config.toml").write_text(setting + "\n")
    with pytest.raises(auth.AuthError, match="^auth_store_invalid$"):
        with auth.isolated_credentials(source, target):
            pytest.fail("invalid storage settings")


@pytest.fixture
def linux_native(monkeypatch):
    """Model native libsecret pointers and the original item identity."""
    state = {"attributes": {}, "updated": [], "released": [], "legacy": False, "duplicate": False}
    value = json.dumps(credential()).encode()
    buffer = ctypes.create_string_buffer(value)
    tail = auth._GList(778, None, None)
    head = auth._GList(777, None, None)
    def search(service, schema, table, flags, cancellable, error):
        """Return unique, legacy, or ambiguous native keyring fixtures."""
        assert flags == 14 and state["attributes"][b"service"] == b"Codex Auth"
        assert state["attributes"][b"username"] == b"cli|test"
        if state["legacy"] and b"target" in state["attributes"]:
            return None
        head.next = ctypes.addressof(tail) if state["duplicate"] else None
        return ctypes.addressof(head)
    def data(secret, length):
        """Expose fixture bytes through the libsecret length output pointer."""
        ctypes.cast(length, ctypes.POINTER(ctypes.c_size_t))[0] = len(value)
        return ctypes.addressof(buffer)
    def create(password, size, content_type):
        """Model a native SecretValue that owns the refreshed bytes."""
        assert password == value and size == len(value) and content_type == b"text/plain"
        return 99
    def update(item, secret, cancellable, error):
        """Record updates to the existing item rather than creating a duplicate."""
        state["updated"].append(item)
        assert item == 777  # Existing item, irrespective of its collection.
        return 1
    def insert(table, key, attribute):
        """Retain the attributes sent to the native keyring search."""
        state["attributes"][key] = attribute
    native = SimpleNamespace(secret_service_get_sync=lambda *args: 11,
        secret_service_search_sync=search, secret_collection_for_alias_sync=lambda *args: 12,
        secret_collection_search_sync=search, secret_item_get_secret=lambda *args: 98,
        secret_value_get=data, secret_value_new=create, secret_item_set_secret_sync=update,
        secret_value_unref=lambda *args: None)
    glib = SimpleNamespace(g_hash_table_new=lambda *args: 13, g_hash_table_insert=insert,
        g_hash_table_remove=lambda table, key: state["attributes"].pop(key),
        g_hash_table_unref=lambda *args: None, g_list_free=lambda *args: None,
        g_error_free=lambda *args: None,
        g_str_hash=ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(lambda *args: 0),
        g_str_equal=ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda *args: 1))
    objects = SimpleNamespace(g_object_unref=lambda item: state["released"].append(item))
    gio = SimpleNamespace(g_cancellable_new=lambda: 14, g_cancellable_cancel=lambda *args: None)
    libraries = {"secret-1": native, "glib-2.0": glib, "gobject-2.0": objects, "gio-2.0": gio}
    monkeypatch.setattr(auth.ctypes.util, "find_library", lambda name: name)
    monkeypatch.setattr(auth.ctypes, "CDLL", lambda name: libraries[name])
    return state, value


def test_linux_native_updates_actual_item_instead_of_default_collection(linux_native):
    """Linux native updates actual item instead of default collection."""
    state, value = linux_native
    assert auth._linux_keyring("cli|test") == value
    auth._linux_keyring("cli|test", value)
    assert state["updated"] == [777]
    assert state["attributes"][b"target"] == b"default"
    assert state["released"] == [14, 777, 11, 14, 777, 11]


def test_linux_native_legacy_default_collection_fallback(linux_native):
    """Linux native legacy default collection fallback."""
    state, value = linux_native
    state["legacy"] = True
    assert auth._linux_keyring("cli|test") == value
    assert b"target" not in state["attributes"] and 12 in state["released"]


def test_linux_ambiguous_keyring_is_rejected(linux_native):
    """Linux ambiguous keyring is rejected."""
    state, _ = linux_native
    state["duplicate"] = True
    with pytest.raises(auth.AuthError, match="auth_keyring_ambiguous"):
        auth._linux_keyring("cli|test")
    assert state["released"] == [14, 777, 778, 11]


def test_mac_native_keyring_selects_user_domain_and_releases_buffers(monkeypatch):
    """Mac native keyring selects user domain and releases buffers."""
    value = json.dumps(credential()).encode()
    buffer = ctypes.create_string_buffer(value)
    released = []
    def domain(domain, pointer):
        """Return the macOS default user keychain fixture pointer."""
        assert domain == 0
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 1
        return 0
    def find(chain, size, service, account_size, account, length, data, item):
        """Expose a synthetic macOS generic-password item and its buffer."""
        assert chain.value == 1 and service == b"Codex Auth" and account == b"cli|test"
        ctypes.cast(length, ctypes.POINTER(ctypes.c_uint32))[0] = len(value)
        ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.cast(buffer, ctypes.c_void_p)
        ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0] = 2
        return 0
    def modify(item, attributes, size, password):
        """Check an in-place update to the existing macOS keychain item."""
        assert item.value == 2 and password == value and size == len(value)
        return 0
    security = SimpleNamespace(SecKeychainCopyDomainDefault=domain, SecKeychainFindGenericPassword=find,
        SecKeychainSetUserInteractionAllowed=lambda allowed: 0,
        SecKeychainItemModifyAttributesAndData=modify, SecKeychainItemFreeContent=lambda *args: released.append("buffer"))
    core = SimpleNamespace(CFRelease=lambda item: released.append(item.value))
    monkeypatch.setattr(auth.ctypes.util, "find_library", lambda name: name)
    monkeypatch.setattr(auth.ctypes, "CDLL", lambda name: security if name == "Security" else core)
    assert auth._mac_keyring("cli|test") == value
    auth._mac_keyring("cli|test", value)
    assert released == ["buffer", 2, 1, "buffer", 2, 1]


def test_symlink_credentials_and_lock_are_rejected(homes, tmp_path):
    """Symlink credentials and lock are rejected."""
    source, target = homes
    other = tmp_path / "other"
    write(other, credential())
    (source / "auth.json").symlink_to(other)
    with pytest.raises(auth.AuthError, match="auth_symlink"):
        with auth.isolated_credentials(source, target):
            pytest.fail("credential symlink")
    (source / "auth.json").unlink()
    write(source / "auth.json", credential())
    (source / ".autoharness-auth.lock").symlink_to(other)
    with pytest.raises(OSError):
        with auth.isolated_credentials(source, target):
            pytest.fail("lock symlink")
