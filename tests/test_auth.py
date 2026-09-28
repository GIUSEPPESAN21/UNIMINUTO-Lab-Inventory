# -*- coding: utf-8 -*-
from core import auth


def test_is_institutional_email_accepts_edu_and_edu_co_domains():
    assert auth.is_institutional_email("ana@uniminuto.edu.co")
    assert auth.is_institutional_email("Juan@Uniminuto.edu.co")
    # No esta restringido a una sola institucion: cualquier dominio
    # academico terminado en .edu o .edu.co es valido.
    assert auth.is_institutional_email("ana@uniandes.edu.co")
    assert auth.is_institutional_email("student@mit.edu")


def test_is_institutional_email_rejects_non_edu_domains():
    assert not auth.is_institutional_email("ana@gmail.com")
    assert not auth.is_institutional_email("no-es-un-correo")
    assert not auth.is_institutional_email("")
    assert not auth.is_institutional_email("ana@notedu.co")


def test_password_hash_roundtrip():
    hashed = auth.hash_password("clave-segura-123")
    assert auth.verify_password("clave-segura-123", hashed)
    assert not auth.verify_password("otra-clave", hashed)


def test_register_user_defaults_to_estudiante(storage):
    user, error = auth.register_user(
        storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing. de Sistemas", "TI2024001"
    )
    assert error is None
    assert user["role"] == "estudiante"
    assert user["student_id"] == "TI2024001"


def test_register_user_whitelisted_email_becomes_profesor(storage):
    storage.add_to_whitelist("prof@uniminuto.edu.co")
    user, error = auth.register_user(
        storage, "Prof. Gomez", "prof@uniminuto.edu.co", "password123", "Ing. Industrial", "DOC2024001"
    )
    assert error is None
    assert user["role"] == "profesor"


def test_register_user_rejects_non_institutional_email(storage):
    user, error = auth.register_user(storage, "Ana Perez", "ana@gmail.com", "password123", "Ing.", "TI001")
    assert user is None
    assert "institucional" in error.lower()


def test_register_user_rejects_incomplete_name(storage):
    user, error = auth.register_user(
        storage, "Ana", "ana@uniminuto.edu.co", "password123", "Ing.", "TI001"
    )
    assert user is None
    assert "nombre" in error.lower()


def test_register_user_rejects_missing_student_id(storage):
    user, error = auth.register_user(
        storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing.", ""
    )
    assert user is None
    assert "id de estudiante" in error.lower()


def test_register_user_rejects_missing_program(storage):
    user, error = auth.register_user(
        storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "", "TI001"
    )
    assert user is None
    assert "programa" in error.lower()


def test_register_user_rejects_short_password(storage):
    user, error = auth.register_user(storage, "Ana Perez", "ana@uniminuto.edu.co", "123", "Ing.", "TI001")
    assert user is None
    assert "contrasena" in error.lower()


def test_register_user_rejects_duplicate_email(storage):
    auth.register_user(storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing.", "TI001")
    user, error = auth.register_user(storage, "Ana P2", "ana@uniminuto.edu.co", "password123", "Ing.", "TI002")
    assert user is None
    assert "ya existe" in error.lower()


def test_login_user_wrong_password(storage):
    auth.register_user(storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing.", "TI001")
    user, error = auth.login_user(storage, "ana@uniminuto.edu.co", "clave-equivocada")
    assert user is None
    assert error is not None


def test_login_user_disabled_account(storage):
    created, _ = auth.register_user(storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing.", "TI001")
    storage.update_user(created["id"], {"status": "disabled"})
    user, error = auth.login_user(storage, "ana@uniminuto.edu.co", "password123")
    assert user is None
    assert "deshabilitada" in error.lower()


def test_login_user_success(storage):
    auth.register_user(storage, "Ana Perez", "ana@uniminuto.edu.co", "password123", "Ing.", "TI001")
    user, error = auth.login_user(storage, "ana@uniminuto.edu.co", "password123")
    assert error is None
    assert user["institutional_email"] == "ana@uniminuto.edu.co"
