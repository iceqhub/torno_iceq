TORNO_ICEQ - PATCH 1.19 VALIDADO

Objetivo:
- corrigir a base de hardware da 7i96S
- parar o erro de pin inexistente no inputs.hal
- separar claramente:
  - carregamento da placa
  - sinais de input
  - IO map

Decisão desta correção:
- criar hardware_7i96s.hal
- incluir este HAL no .ini
- substituir inputs.hal por versão mínima e segura
- NÃO mexer ainda em encoder, stepgen e outputs físicos além do necessário

Escopo desta versão:
- LinuxCNC subir sem erro de naming básico da 7i96S
- validar E-stop e Cycle Start primeiro

halcmd show pin | grep -i "axis\|limit" | grep -v "step\|dir\|enc\|home\|inm\|gpio"