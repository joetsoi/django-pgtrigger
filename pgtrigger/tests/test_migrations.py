"""Tests behavior related to migrations"""
import pathlib
import shutil

import ddf
from django.apps import apps
import django.contrib.auth.models as auth_models
from django.core.management import call_command
from django.db import models
from django.db.utils import InternalError, ProgrammingError
import pytest

import pgtrigger
import pgtrigger.core
import pgtrigger.tests.models as test_models


@pytest.fixture
def reset_triggers():
    """Ensures all triggers are uninstalled before the tests"""
    pgtrigger.uninstall(database="default")

    yield

    pgtrigger.install(database="default")


@pytest.fixture
def reset_migrations(tmp_path):
    """Ensures the migration dir is reset after the test"""
    migration_dir = pathlib.Path(__file__).parent / "migrations"
    num_migrations = len(list(migration_dir.glob("0*.py")))
    shutil.copytree(migration_dir, tmp_path / "migrations")

    yield

    # Migrate back to the initial migration of the test to allevitate
    # some of the issues when re-using a test DB
    call_command("migrate", "tests", str(num_migrations).rjust(4, "0"))

    shutil.rmtree(migration_dir)
    shutil.copytree(tmp_path / "migrations", migration_dir)


def num_files(dir_path):
    return len(list(dir_path.iterdir()))


def assert_all_triggers_installed():
    for model, trigger in pgtrigger.core.get(database="default"):
        status = trigger.get_installation_status(model)
        assert status[0] == pgtrigger.core.INSTALLED


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("reset_triggers", "reset_migrations")
@pytest.mark.order(-1)  # This is a possibly leaky test if it fails midway. Always run last
def test_makemigrations_existing_models(settings):
    """
    Create migrations for existing models and test various scenarios
    where triggers are dynamically added and removed
    """
    # Verify that we've configured our test settings properly
    assert not settings.PGTRIGGER_INSTALL_ON_MIGRATE
    assert settings.PGTRIGGER_MIGRATIONS

    migration_dir = pathlib.Path(__file__).parent / "migrations"
    num_orig_migrations = num_files(migration_dir)

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 1

    call_command("migrate")
    assert_all_triggers_installed()

    # Add a new trigger to the registry that should be migrated
    trigger = pgtrigger.Trigger(
        when=pgtrigger.Before,
        name='my_migrated_trigger',
        operation=pgtrigger.Insert | pgtrigger.Update,
        func="RAISE EXCEPTION 'no no no!';",
    )

    with trigger.register(test_models.TestModel):
        call_command("makemigrations")
        assert num_files(migration_dir) == num_orig_migrations + 2

        # As a sanity check, ensure makemigrations doesnt make dups
        call_command("makemigrations")
        assert num_files(migration_dir) == num_orig_migrations + 2

        # Before migrating, I should be able to make a ``TestModel``
        ddf.G("tests.TestModel")

        call_command("migrate")
        assert_all_triggers_installed()

        # After migrating, test models should be protected
        with pytest.raises(InternalError, match='no no no!'):
            test_models.TestModel.objects.create()

        # Update the trigger to allow inserts, but not updates.
        # We should have a new migration
        trigger.operation = pgtrigger.Update
        call_command("makemigrations")
        assert num_files(migration_dir) == num_orig_migrations + 3

        call_command("migrate")
        assert_all_triggers_installed()

        # We should be able to make test models but not update them
        test_model = ddf.G("tests.TestModel")
        with pytest.raises(InternalError, match='no no no!'):
            test_model.save()

    # The trigger is now removed from the registry. It should create
    # a new migration
    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 4

    call_command("migrate")
    assert_all_triggers_installed()

    # We should be able to create and update the test model now that
    # the trigger is removed
    test_model = ddf.G("tests.TestModel")
    test_model.save()

    # Create a protection trigger on the external user model and
    # migrate it
    trigger = pgtrigger.Trigger(
        when=pgtrigger.Before,
        name='nothing_allowed',
        operation=pgtrigger.Insert | pgtrigger.Update,
        func="RAISE EXCEPTION 'no no no!';",
    )

    # Test that special characters migrate correctly
    trigger = pgtrigger.Protect(
        name='special_characters',
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(new__char_field="%"),
    )
    with trigger.register(test_models.TestModel):
        call_command("makemigrations")
        assert num_files(migration_dir) == num_orig_migrations + 5

        call_command("migrate")
        assert_all_triggers_installed()

        tm = ddf.G("tests.TestModel", char_field="hello")

        with pytest.raises(InternalError, match='Cannot update rows'):
            tm.char_field = "%"
            tm.save()


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("reset_triggers", "reset_migrations")
@pytest.mark.order(-1)  # This is a possibly leaky test if it fails midway. Always run last
@pytest.mark.independent  # Run independently of test suite since it creates models and issues
def test_makemigrations_create_remove_models(settings):
    """
    Tests migration scenarios where models are dynamically added and
    removed.
    """
    assert not settings.PGTRIGGER_INSTALL_ON_MIGRATE
    assert settings.PGTRIGGER_MIGRATIONS

    migration_dir = pathlib.Path(__file__).parent / "migrations"
    num_orig_migrations = num_files(migration_dir)

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 1

    call_command("migrate")
    assert_all_triggers_installed()

    # Create a new model and add it to the test models
    class DynamicTestModel(models.Model):
        field = models.IntegerField()
        user = models.ForeignKey(auth_models.User, on_delete=models.CASCADE)

        class Meta:
            triggers = [
                pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete),
                pgtrigger.Protect(name="protect_updates", operation=pgtrigger.Update),
            ]

    test_models.DynamicTestModel = DynamicTestModel

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 2
    call_command("migrate")
    assert_all_triggers_installed()

    # Sanity check that we cannot delete or update a DynamicTestModel
    protected_model = ddf.G(test_models.DynamicTestModel)

    with pytest.raises(InternalError, match="Cannot update"):
        protected_model.field += 1
        protected_model.save()

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.delete()

    # Keep only deletion protection and migrate
    DynamicTestModel._meta.triggers = [
        pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete)
    ]
    DynamicTestModel._meta.original_attrs["triggers"] = DynamicTestModel._meta.triggers

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 3
    call_command("migrate")
    assert_all_triggers_installed()

    # Updates work, but deletes dont
    protected_model.field += 1
    protected_model.save()

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.delete()

    # Remove the model and verify it migrates
    del test_models.DynamicTestModel
    del apps.app_configs["tests"].models["dynamictestmodel"]
    apps.clear_cache()

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 4
    call_command("migrate")
    assert_all_triggers_installed()

    with pytest.raises(ProgrammingError):
        protected_model.delete()

    # Create a new proxy model on a third-party app and add it to the test models
    class DynamicProxyModel(auth_models.User):
        class Meta:
            proxy = True
            triggers = [
                pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete),
                pgtrigger.Protect(name="protect_updates", operation=pgtrigger.Update),
            ]

    test_models.DynamicProxyModel = DynamicProxyModel

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 5
    call_command("migrate")
    assert_all_triggers_installed()

    # Sanity check that we cannot delete or update a user
    protected_model = ddf.G(auth_models.User)

    with pytest.raises(InternalError, match="Cannot update"):
        protected_model.username = "wes"
        protected_model.save()

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.delete()

    # Keep only deletion protection and migrate
    DynamicProxyModel._meta.triggers = [
        pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete)
    ]
    DynamicProxyModel._meta.original_attrs["triggers"] = DynamicProxyModel._meta.triggers

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 6
    call_command("migrate")
    assert_all_triggers_installed()

    # Updates work, but deletes dont
    protected_model.username = "wes"
    protected_model.save()

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.delete()

    # Remove the model and verify it migrates
    del test_models.DynamicProxyModel
    del apps.app_configs["tests"].models["dynamicproxymodel"]
    apps.clear_cache()

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 7
    call_command("migrate")
    assert_all_triggers_installed()

    # We can delete the original model
    protected_model.delete()

    # Create a new unmanaged model on auth_models.User group relationships and add it
    # to the test models
    class DynamicUnmanagedModel(models.Model):
        class Meta:
            managed = False
            db_table = auth_models.User.groups.through._meta.db_table
            triggers = [
                pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete),
                pgtrigger.Protect(name="protect_inserts", operation=pgtrigger.Insert),
            ]

    test_models.DynamicUnmanagedModel = DynamicUnmanagedModel

    # Sanity check that we cannot insert or delete a group
    protected_model = ddf.G(auth_models.User)
    protected_model.groups.add(ddf.G(auth_models.Group))

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 8
    call_command("migrate")
    assert_all_triggers_installed()

    with pytest.raises(InternalError, match="Cannot insert"):
        protected_model.groups.add(ddf.G(auth_models.Group))

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.groups.clear()

    # Keep only deletion protection and migrate
    DynamicUnmanagedModel._meta.triggers = [
        pgtrigger.Protect(name="protect_deletes", operation=pgtrigger.Delete)
    ]
    DynamicUnmanagedModel._meta.original_attrs["triggers"] = DynamicUnmanagedModel._meta.triggers

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 9
    call_command("migrate")
    assert_all_triggers_installed()

    # Inserts work, but deletes dont
    protected_model.groups.add(ddf.G(auth_models.Group))

    with pytest.raises(InternalError, match="Cannot delete"):
        protected_model.groups.clear()

    # Remove the model and verify it migrates
    del test_models.DynamicUnmanagedModel
    del apps.app_configs["tests"].models["dynamicunmanagedmodel"]
    apps.clear_cache()

    call_command("makemigrations")
    assert num_files(migration_dir) == num_orig_migrations + 10
    call_command("migrate")
    assert_all_triggers_installed()

    # We can delete the groups
    protected_model.groups.clear()