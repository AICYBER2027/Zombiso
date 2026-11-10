"""VicePulse: short-form GTA media pipeline for VicePulse EN and VicePulse BR.

Every stage is a plain function plus a CLI subcommand (see ``vicepulse.cli``) so n8n
can call it. Nothing in this package publishes, spends money or calls Claude unless
the safety switches in ``.env`` are changed on purpose.
"""

__version__ = "0.1.0"
