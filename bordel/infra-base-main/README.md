# Matomo

Ce chart permet de déployer une instance Matomo qui embarque une base de données Mariadb. Il reprend le chart [bitnami](https://github.com/bitnami/charts/blob/main/bitnami/matomo/README.md).

### Gestion des secrets

La gestion des secrets n'est pas incluse, il faut les déployer via un autre repos d'infrastructure via sops/vault.
Sur le projet actuel (OVH/MI) un repos secret est présent.

Voici un exemple des secrets à renseigner:

```yaml
apiVersion: isindir.github.com/v1alpha3
kind: SopsSecret
metadata:
    name: matomo-secrets
spec:
    secretTemplates:
        - name: mariadb-creds
          type: Opaque
          stringData:
            mariadb-password: "nvm"
            mariadb-root-password: "nvm"
        - name: matomo-creds
          type: Opaque
          stringData:
            matomo-password: "nvm"
        - name: s3-creds
          type: Opaque
          stringData:
            AWS_ACCESS_KEY_ID: "ID"
            AWS_SECRET_ACCESS_KEY: "KEY"
```

### Sauvegarde de la base 

Ce chart comprend un cronjob permettant d'effectuer un backup de la base et de l'envoyer vers S3.

Attention, cela demande de construire l'image docker associée.

Voici le Dockerfile:

```yaml
FROM alpine:3.20.3

RUN apk update && apk add aws-cli mysql-client && apk cache clean

ENTRYPOINT ["/bin/ash"]
```

### Restauration de la base 

Il n'y a pas de job de restauration pour le moment, mais vous pouvez éditer le job de backup comme ci-dessous.
Et le lancer manuellement depuis ArgoCD.  

```yaml
echo "Begin Restore"

cd /data

aws s3 cp s3://$S3_BUCKET/matomo-backup-database-2025-03-07-06.00.04.sql.tar.gz matomo-backup-database-2025-03-07-06.00.04.sql.tar.gz --endpoint $S3_ENDPOINT --no-verify-ssl

tar -xvzf matomo-backup-database-2025-03-07-06.00.04.sql.tar.gz

mysql -h $DB_HOST -u $DB_USER $DB_NAME -pDBPASSWORD < matomo_backup_database.sql

```