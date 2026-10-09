import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class CareersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "careers"

    def ready(self):
        """
        Registers the bots this deployment ships with.

        The shared package is preferred and the in-repo adapter is the
        fallback, in that order, because a deployment that has `kormic_agents`
        installed should be using it — the in-repo one wraps a resume parser
        written for graduate admissions, and on a nurse's CV it looks for a GRE
        score.

        Neither is allowed to stop the app starting. A registry with nothing in
        it means every rung stays self_attested, which is the honest state for
        a deployment whose bots are not installed.
        """
        from django.conf import settings

        from .verifiers_kormic import register_kormic_agents

        # Only in development, and only ever fixtures. A deployment with DEBUG
        # off gets an empty directory, so every credential reports honestly
        # that no register was reached — which is the true state of things
        # until one is integrated.
        directory = None
        if settings.DEBUG:
            from .authorities_dev import build_directory

            directory = build_directory()

        # Real registers, in every deployment. Six states publish their nursing
        # register as open data — TX, IL, WA, CO, CT, DE — and a licence from
        # one of them is confirmed with the state itself. Every other state
        # stays honestly unchecked until it has a route.
        try:
            from kormic_agents.credentials import Directory, register_nursing_licences

            if directory is None:
                directory = Directory()
            states = register_nursing_licences(directory)
            logger.info("careers: state licence registers wired for %s", ", ".join(states))
        except ImportError:
            logger.info("careers: no state licence registers (kormic_agents not installed)")

        # The OIG list, if `manage.py fetch_leie` has ever run. Without one the
        # `oig` rung stays unwired — an exclusion check with no list would look
        # like a check that ran. Later months are picked up without a restart.
        from .leie import LatestLeie

        leie = LatestLeie(settings.LEIE_DIR)
        if register_kormic_agents(directory, leie=leie if leie.available() else None):
            return

        try:
            from .verifiers_resume import register_builtin_verifiers

            register_builtin_verifiers()
        except Exception as exc:  # noqa: BLE001
            logger.info("careers: no verifiers registered (%s)", exc)
