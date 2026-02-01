# /Users/etiquet/Documents/GitHub
helm dependency build
helm lint .
helm template .

# Description des valeurs du chart

app.name: Nom de l'application déployée.
app.replicaCount: Nombre de réplicas pour le déploiement.
app.image.repository: Référentiel DockerHub fictif pour l'image de l'application.
app.image.tag: Tag de l'image Docker fictive.
app.imagePullSecrets: Secret fictif pour l'accès à DockerHub.
app.s3Directory: Nom de répertoire fictif pour les fichiers S3.
app.s3UseHttp: Utilisation fictive de HTTP pour S3 (ici, configuré sur "false").
app.s3SkipVerify: Validation fictive du certificat pour S3 (ici, configuré sur "false").
app.s3Webhook: URL de webhook fictive.
app.webhookSkipVerify: Validation fictive du certificat pour le webhook (ici, configuré sur "false").
app.host: Domaine fictif pour l'application.
nginx.enabled: Activation du déploiement Nginx Bitnami (ici, configuré sur "true").
nginx.image.registry: Référentiel pour l'image Nginx.
nginx.image.repository: Référentiel Docker pour l'image Nginx.
nginx.image.tag: Tag de l'image Nginx.
nginx.service.type: Type de service pour Nginx (ici, configuré sur "LoadBalancer").
nginx.service.port: Port pour le service Nginx.
nginx.ingress.enabled: Activation de l'Ingress pour Nginx (ici, configuré sur "true").
nginx.ingress.annotations: Annotations pour l'Ingress Nginx.
nginx.ingress.hostname: Nom d'hôte fictif pour Nginx.
nginx.ingress.path: Chemin pour l'Ingress Nginx.
nginx.ingress.tls: Configuration fictive TLS pour l'Ingress Nginx (ici, configuré sur "false").
