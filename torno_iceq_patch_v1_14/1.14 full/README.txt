TORNO_ICEQ - CONTROLE DE VERSÕES

Versão atual: 1.14

-----------------------------------
Versão 1.1 → 1.13
-----------------------------------
- Estrutura completa criada
- Dispatcher funcional
- HAL base + inputs 7i96S
- Início do mapeamento real da máquina

-----------------------------------
Versão 1.14
-----------------------------------
- Removida a premissa de spindle por inversor
- Projeto atualizado para spindle principal por servo
- Preparação do spindle principal para:
  - RPM agora
  - eixo C angular no futuro
- Inclusão estrutural do eixo B (ferramenta acionada)
- Parâmetros claros para ajuste de:
  - RPM do eixo B
  - relação de polia do eixo B
  - limites de velocidade do eixo B
- Atualização do arquivo de mapeamento para:
  - spindle-servo
  - eixo B
- Mantido sem travamento automático entre B e C
  (controle livre por G-code, conforme decisão do projeto)

-----------------------------------
DADOS ATUAIS DO SPINDLE PRINCIPAL
-----------------------------------
Servo motor:
HLTNC 130ST-M15025
Potência: 3.8 kW
Rotação nominal: 2500 RPM

Driver:
HLTNC HL-T3DF-L30F-RABF-B

Transmissão por polia:
Polia motor: 65 mm
Polia spindle: 145 mm
Relação: 145 / 65 = 2.23

RPM máximo estimado no spindle:
2500 / 2.23 = ~1120 RPM

Encoder de spindle:
Omron E6B2-CWZ5B (mantido para sincronismo)

-----------------------------------
DADOS DO EIXO B
-----------------------------------
Função:
- Retífica interna
- Furação até ~10 mm
- Rosqueamento M6
- Fresamento leve

Arquitetura:
- X = linear
- Z = linear
- A = magazine
- C = spindle principal
- B = ferramenta acionada

Estado atual do eixo B:
- Servo provisório de 1 kW
- Controle previsto via step/dir
- Estrutura preparada para RPM agora
- Futuro posicionamento angular documentado

-----------------------------------
ONDE AJUSTAR O EIXO B
-----------------------------------
No arquivo TORNO_ICEQ.ini:
- [AXIS_B]
- [JOINT_3]

No arquivo io_map.hal:
- setp spindle-b-rpm-scale.gain
- setp spindle-b-polia-ratio.gain
- setp spindle-b-max-rpm.gain

-----------------------------------
OBSERVAÇÃO IMPORTANTE
-----------------------------------
A preparação para spindle híbrido (RPM + angular) do eixo C
NÃO está finalizada nesta versão.
Esta versão deixa pronta a estrutura para a próxima etapa.

Para finalizar o eixo C híbrido depois, ainda vai faltar:
- identificar o pino real de troca de modo no driver do spindle
- implementar a lógica HAL de troca de modo
- fechar a configuração do LinuxCNC para spindle/eixo C

-----------------------------------
PRÓXIMOS PASSOS
-----------------------------------
- Comissionar E-stop
- Comissionar spindle servo em RPM
- Validar encoder do spindle
- Fechar mapeamento físico fio por fio
- Iniciar ativação do eixo C híbrido
