import logging
logger = logging.getLogger(__name__)

import abc
import re
from functools import wraps

from stevedore import extension
from orderedattrdict import AttrDict

# from .. import session
from .. import config
from ..exceptions import *

PROVIDERS = AttrDict()
DEFAULT_PROVIDER=None

def get(provider, *args, **kwargs):
    try:
        return PROVIDERS.get(provider)
    except TypeError:
        raise Exception(provider, PROVIDERS)

MEDIA_SPEC_RE=re.compile(r"([^:/]*)(?:/([^:]+))?(?::(.*))?")

def parse_spec(spec):

    if not spec:
        spec = DEFAULT_PROVIDER

    (provider, identifier, options) = MEDIA_SPEC_RE.search(spec).groups()

    if not provider:
        provider = DEFAULT_PROVIDER

    p = get(provider)

    if not p:
        raise Exception(f"provider {provider} not found")

    options = p.parse_options(options)
    for k, v in options.items():
        if k in p.filters:
            p.filters[k].value = v

    try:
        selection = p.parse_identifier(identifier)
    except SGIncompleteIdentifier as e:
        return (p, None, options)
    return (p, selection, options)


def log_plugin_exception(manager, entrypoint, exception):
    logger.error('Failed to load %s: %s' % (entrypoint, exception))

def load():
    global PROVIDERS
    global DEFAULT_PROVIDER
    mgr = extension.ExtensionManager(
        namespace='streamglob.providers',
        on_load_failure_callback=log_plugin_exception,
    )
    PROVIDERS = AttrDict(
        (x.name, x.plugin())
        for x in mgr
    )

    if len(config.settings.profile.providers):
        # first listed in config
        DEFAULT_PROVIDER = list(config.settings.profile.providers.keys())[0]
    else:
        # first loaded
        DEFAULT_PROVIDER = list(PROVIDERS.keys())[0]
