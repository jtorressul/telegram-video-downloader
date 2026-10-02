"""Run on the server: shows egress IP/ASN per route and which platforms are blocked.

Usage: python diag.py
"""
import config
from downloaders.diag import run_diagnostics

if __name__ == "__main__":
    print(f"ANONYMOUS_ONLY={config.ANONYMOUS_ONLY}  IPV6_PREFIX={config.IPV6_PREFIX or '-'}  "
          f"WARP_PROXY={config.WARP_PROXY or '-'}  PROXY={'sí' if config.PROXY else '-'}  "
          f"COBALT={len(config.COBALT_INSTANCES)} instancias")
    print(run_diagnostics())
