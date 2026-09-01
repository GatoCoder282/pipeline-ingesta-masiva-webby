# Product Spec - Ingesta documental local para Webby

## Problema

Las tiendas entregan catalogos en formatos y convenciones distintas. La
herramienta debe reducir el trabajo manual de entender documentos, extraer
productos y variantes, corregir calidad y conservar trazabilidad.

## Usuario primario

La persona que opera migraciones y configuraciones de tenants de Webby. En la
V1 es un operador tecnico unico, no un servicio publico multiusuario.

## Objetivo V1

Procesar un catalogo visual de una tienda desde PDF o imagen, generar artefactos
locales con evidencia por pagina/bloque, estructurar productos y variantes,
mostrar errores por registro y dejar un JSON canonico + CSV listos para revisar.
La publicacion a Webby queda como integracion opcional posterior.

## Fuera de alcance V1

- escritura directa a la base de datos de Webby;
- publicacion automatica en produccion;
- importacion de clientes como primer caso;
- scraping autonomo no supervisado;
- agente que apruebe o publique por si mismo;
- dependencia obligatoria de n8n, MinIO o dbt;
- OCR cloud obligatorio o envio automatico de documentos a terceros.

## Criterios de aceptacion

1. Una segunda tienda puede configurarse con un perfil sin editar el nucleo.
2. El archivo original se conserva con checksum y `run_id`.
3. Los errores de tipo, requeridos, duplicados y valores invalidos aparecen por registro.
4. El lote invalido no puede aprobarse ni publicarse.
5. Un catalogo visual conserva texto, pagina, coordenadas y confianza.
6. Productos y variantes se entregan como registros canonicos separados.
7. La ejecucion local y la ejecucion desde Kestra usan el mismo comando de CLI.
