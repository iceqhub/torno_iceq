TORNO_ICEQ - CONTROLE DE VERSÕES

Versão atual: 1.16

-----------------------------------
REVISÃO GERAL FEITA NA 1.16
-----------------------------------
Esta versão foi gerada após revisão dos arquivos atuais do repositório
torno_iceq e comparação com a base funcional da máquina antiga.

Foram identificados e corrigidos os seguintes problemas acumulados:
- torno_iceq.ini estava com COORDINATES e JOINTS inconsistentes
- torno_iceq.ini não incluía todos os HALFILE já criados
- eixo A (magazine) ainda não estava formalizado no .ini novo
- eixo B estava incluído parcialmente
- inputs.hal e io_map.hal tinham sobreposição de responsabilidade
- ui_bridge.hal existia mas ainda não estava entrando pelo .ini
- io.hal ficou obsoleto para a configuração atual

-----------------------------------
Versão 1.16
-----------------------------------
- .ini refeito de forma coerente e completa
- Estrutura atual válida:
  - X = linear
  - Z = linear
  - A = magazine
  - B = ferramenta acionada
- Spindle principal por servo mantido fora de coordenadas por enquanto
- Eixo C mantido apenas como preparação futura
- HALFILEs organizados corretamente:
  - motion.hal
  - inputs.hal
  - ui_bridge.hal
  - io_map.hal
- Eixo A reconstruído com base da máquina antiga
- Eixos X e Z preservados com os valores da máquina antiga
- Eixo B mantido parametrizado e documentado

-----------------------------------
ESTADO DA MÁQUINA APÓS 1.16
-----------------------------------
Pronta para:
- teste de jog X
- teste de jog Z
- teste de magazine A
- comissionamento do E-stop
- comissionamento do spindle por servo em RPM

Ainda NÃO finalizado:
- eixo C híbrido
- ligação física definitiva fio por fio
- MPG final
- tela final equivalente à antiga

-----------------------------------
PRÓXIMOS PASSOS
-----------------------------------
- testar leitura do LinuxCNC sem erro de configuração
- testar jog X/Z
- testar E-stop
- confirmar pino real do enable do spindle-servo
- avançar para pacote 1.17 focado em comissionamento
