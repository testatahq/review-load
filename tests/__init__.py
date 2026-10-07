"""Tests never touch the network: any real HTTP call fails loudly."""

import urllib.request


def _no_network(*args, **kwargs):
    raise AssertionError("tests must not use the network")


urllib.request.urlopen = _no_network
