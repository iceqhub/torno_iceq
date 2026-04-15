TORNO_ICEQ - CONTROLE DE VERSÕES

Versão atual: 1.18-atualizado

-----------------------------------
Versão 1.18-atualizado
-----------------------------------
- Atualização documental do comissionamento inicial
- Esclarecida a diferença entre:
  - PE / terra de proteção
  - 0V / GND da fonte 24V
- Confirmado o uso dos blocos:
  - verde = PE
  - preto = 0V / GND 24V
  - vermelho = +24V
- Adicionadas regras de roteamento nas calhas:
  - direita = potência
  - esquerda = sinais
- Atualizado o mapa inicial de ligação

-----------------------------------
REGRA FUNDAMENTAL
-----------------------------------
PE (terra de proteção) NÃO é retorno de 24V.
0V/GND de 24V NÃO é a mesma coisa que PE.

Pode existir ligação entre 0V e PE em UM único ponto, se necessário,
mas o 0V continua sendo distribuição separada do PE.

-----------------------------------
ESTA VERSÃO AINDA COBRE SOMENTE
-----------------------------------
- alimentação da 7i96S
- E-stop
- Cycle Start
- enable básico do spindle servo
- encoder do spindle

-----------------------------------
PRÓXIMO PASSO
-----------------------------------
- 1.19 = X / Z / homes
