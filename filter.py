
import sys
import fbf

EXPECTED = "3.14"
CURRENT = f"{sys.version_info.major}.{sys.version_info.minor}"

if CURRENT != EXPECTED:
    sys.exit(
        f"[!] This build requires Termux Python {EXPECTED} "
        f"(found {CURRENT})."
    )

if __name__ == "__main__":
    fbf.main()
