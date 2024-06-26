import contextlib
import json
import logging
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.contrib.gis.geoip2 import GeoIP2
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string
from activitylog.middleware.middleware import get_current_user, get_current_request, set_local_details
from activitylog.models import CRUDEvent
from activitylog.settings import DATABASE_ALIAS, LOGGING_BACKEND, REMOTE_ADDR_HEADER, HTTP_SEC_CH_UA, \
    HTTP_SEC_CH_UA_PLATFORM, GNOME_SHELL_SESSION_MODE
from activitylog.utils import get_m2m_field_name, should_propagate_exceptions

logger = logging.getLogger(__name__)
audit_logger = import_string(LOGGING_BACKEND)()


def get_current_user_details():
    user_id = ""
    user_pk_as_string = ""

    with contextlib.suppress(Exception):
        user = get_current_user()
        if user and not isinstance(user, AnonymousUser):
            if getattr(settings, "DJANGO_ACTIVITY_LOG_CHECK_IF_REQUEST_USER_EXISTS", True):
                # validate that the user still exists
                user = get_user_model().objects.get(pk=user.pk)
            user_id, user_pk_as_string = user.id, str(user.pk)

    return user_id, user_pk_as_string


def get_user_location():
    remote_ip = None
    browser = None
    platform = None
    operating_system = None
    with contextlib.suppress(Exception):
        address = set_local_details()
        remote_ip = address.META.get(REMOTE_ADDR_HEADER, None)
        browser = address.META.get(HTTP_SEC_CH_UA, None)
        platform = address.META.get(HTTP_SEC_CH_UA_PLATFORM, None)
        operating_system = address.META.get(GNOME_SHELL_SESSION_MODE, None)

    return remote_ip, browser, platform, operating_system


def log_event(event_type, instance, object_json_repr, **kwargs):
    user_id, user_pk_as_string = get_current_user_details()
    remote_ip, browser, platform, operating_system = get_user_location()

    try:
        g = GeoIP2()
        lat, long = g.lat_lon(remote_ip)
        city = g.city(remote_ip)
        country = g.country(remote_ip)
    except Exception:
        lat = None
        long = None
        city = None
        country = None

    with transaction.atomic(using=DATABASE_ALIAS):
        audit_logger.crud(
            {
                "content_type_id": ContentType.objects.get_for_model(instance).id,
                "datetime": timezone.now(),
                "event_type": event_type,
                "object_id": instance.pk,
                "object_json_repr": object_json_repr or "",
                "object_repr": str(instance),
                "user_id": user_id,
                "remote_ip": remote_ip,
                "browser": browser,
                "platform": platform,
                "latitude": lat,
                "longitude": long,
                "city": city,
                "country": country,
                "operating_system": operating_system,
                "user_pk_as_string": user_pk_as_string,
                **kwargs,
            }
        )


def handle_flow_exception(instance, signal):
    instance_str = ""
    with contextlib.suppress(Exception):
        instance_str = f" instance: {instance}, instance pk: {instance.pk}"

    logger.exception(
        f"activity log had a {signal} exception on CRUDEvent creation.{instance_str}"
    )
    if should_propagate_exceptions():
        raise


def pre_save_crud_flow(instance, object_json_repr, changed_fields):
    try:
        log_event(
            CRUDEvent.UPDATE,
            instance,
            object_json_repr,
            changed_fields=changed_fields,
        )
    except Exception:
        handle_flow_exception(instance, "pre_save")


def post_save_crud_flow(instance, object_json_repr):
    try:
        log_event(
            CRUDEvent.CREATE,
            instance,
            object_json_repr,
        )
    except Exception:
        handle_flow_exception(instance, "pre_save")


def m2m_changed_crud_flow(  # noqa: PLR0913
        action, model, instance, pk_set, event_type, object_json_repr
):
    try:
        if action == "post_clear":
            changed_fields = []
        else:
            changed_fields = json.dumps(
                {get_m2m_field_name(model, instance): list(pk_set)},
                cls=DjangoJSONEncoder,
            )
        log_event(
            event_type,
            instance,
            object_json_repr,
            changed_fields=changed_fields,
        )
    except Exception:
        handle_flow_exception(instance, "pre_save")


def post_delete_crud_flow(instance, object_json_repr):
    try:
        log_event(
            CRUDEvent.DELETE,
            instance,
            object_json_repr,
        )

    except Exception:
        handle_flow_exception(instance, "pre_save")
