# Límites de n8n

n8n es opcional. Aporta valor en integraciones con sistemas externos y eventos:

- recibir un webhook;
- detectar archivos en Drive o Sheets;
- iniciar una ejecución de Kestra;
- enviar reportes a correo, Slack o WhatsApp;
- abrir una tarea de revisión.

No debe contener la lógica canónica de negocio, validaciones críticas ni una
publicación que evite la aprobación. Esa lógica debe permanecer en Python,
versionada, testeada y reutilizable desde CLI/Kestra.

La instancia local usa SQLite interno para pruebas simples. Si se convierte en
un servicio compartido, habrá que configurar credenciales, cifrado, backups,
control de acceso y un backend PostgreSQL independiente.
