# Ribbon Out Fault

The RIBBON OUT alert (group 3, code 2) fires when the ribbon supply
spindle reads empty (or no ribbon is installed) while the printer is
configured for thermal-transfer printing.

To clear:

1. If the printer is running thermal-transfer media, install a fresh
   ribbon and re-thread it through the head and the take-up spindle.
2. If the media is in fact direct-thermal, remove any installed ribbon
   and switch the print mode to Direct Thermal in the front-panel menu.
3. Press FEED to confirm a clean print path; the alert should clear.

The pause that auto-fires alongside ribbon out clears on its own once
the ribbon out condition resolves — no manual resume required.
