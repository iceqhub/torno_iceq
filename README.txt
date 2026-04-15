TORNO_ICEQ - CONTROLE DE VERSÕES

Versão atual: 1.17

-----------------------------------
Versão 1.17
-----------------------------------
- Ativação do E-STOP funcional real
- Integração do enable do spindle servo
- Preparação real para teste de rotação (RPM)
- Segurança mínima aplicada
- Base pronta para primeiro movimento do spindle

-----------------------------------
IMPORTANTE
-----------------------------------
Agora já é possível:
- testar botão de emergência real
- energizar servo do spindle
- validar enable / disable

-----------------------------------
TESTE OBRIGATÓRIO
-----------------------------------
1) Liga LinuxCNC
2) Aperta E-STOP → máquina NÃO pode habilitar
3) Solta E-STOP → máquina habilita

Se isso falhar → PARAR

-----------------------------------
PRÓXIMO PASSO
-----------------------------------
- teste de rotação spindle
- ajuste de direção
- ajuste de escala RPM
