#!/usr/bin/env bash
# Abre a TELA em tela cheia, sem barra nenhuma, no monitor do aparelho.
# No Raspberry Pi coloque no ~/.config/autostart ou chame pelo rc.local.
#
# --window-position move a janela pro segundo monitor: troque 1920 pela
# largura do seu monitor principal.
URL="${1:-http://localhost:8080/}"
POS="${2:-1920,0}"

for NAVEGADOR in chromium-browser chromium google-chrome brave-browser; do
  if command -v "$NAVEGADOR" >/dev/null 2>&1; then
    exec "$NAVEGADOR" \
      --kiosk "$URL" \
      --window-position="$POS" \
      --autoplay-policy=no-user-gesture-required \
      --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
      --check-for-update-interval=31536000 \
      --disable-features=TranslateUI
  fi
done
echo "Nenhum navegador Chromium/Chrome encontrado." >&2
exit 1
