# Workflows n8n

n8n es opcional y debe actuar como capa de integración: recibir un webhook,
detectar un archivo en Drive/Sheets, llamar a Kestra o ejecutar la CLI y enviar
el reporte por correo/Slack.

La transformación, las validaciones y la publicación no deben trasladarse a
nodos Code dispersos en n8n. Para una primera integración se puede llamar a
Kestra por HTTP o ejecutar el comando de validación en un worker controlado.
