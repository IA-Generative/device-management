# ADR-0005 : Build in-cluster — BuildKit rootless plutôt que Kaniko

**Date** : 2026-07-26
**Statut** : En vigueur (0.9.15)
**Auteurs** : eric.tiquet + Claude Code (claude-opus-5[1m])
**Portée** : voie de construction d'image depuis le cluster (`scripts/build-incluster.sh`,
`deploy/k8s/buildkit/`). Ne change rien à `scripts/build-k8s.sh` (buildx), qui reste la
voie de référence pour les releases multi-architectures.

---

## Contexte

Le dépôt disposait d'une seule voie de build : `scripts/build-k8s.sh`, qui construit
depuis un poste avec `docker buildx` et pousse au registre. Elle suppose un Docker local
et fait transiter l'image par le poste — deux hypothèses qui tombent dès qu'on veut
construire depuis un runner CI n'ayant qu'un kubeconfig, ou depuis un lien lent.

Une seconde voie, in-cluster, a donc été ajoutée en 0.9.14 avec **Kaniko**. Le contrôle
qualité qui a suivi a relevé que le projet `GoogleContainerTools/kaniko` est **archivé**
depuis 2025 : dernier commit 2025-06-03, dernière release v1.24.0 (mai 2025). Aucun
correctif ne sortira, y compris de sécurité, et l'image `executor` ne sera plus
reconstruite. Introduire une dépendance morte dans une base qu'on doit porter des années
est précisément ce que la revue devait empêcher.

Trois autres limites de la solution Kaniko étaient assumées faute de mieux : un moteur de
build différent de celui des releases (donc des images seulement *supposées* équivalentes),
l'impossibilité de produire un manifeste multi-architectures, et l'obligation de tourner
en root.

## Options considérées

### Option A — Garder Kaniko, épinglé et documenté comme daté

- Pin sur la dernière release, statut d'archive écrit dans le script et le README.
- **Pour** : aucun travail de bascule ; la voie fonctionne et est vérifiée.
- **Contre** : la dette reste entière et grandit. Une CVE dans l'exécuteur n'aurait aucun
  correctif amont. Les trois limites (moteur distinct, mono-arch, root) demeurent.

### Option B — BuildKit rootless

- `moby/buildkit`, invoqué par `buildctl-daemonless.sh` dans un Job.
- **Pour** : c'est **le moteur de `docker buildx`**, donc déjà celui de `build-k8s.sh` —
  une seule implémentation de build pour les deux voies. Activement maintenu. Mode
  rootless. Multi-arch natif. Cache registre partagé avec les builds buildx.
- **Contre** : impose `seccomp` et `AppArmor` en `Unconfined` (espaces de noms utilisateur
  imbriqués). Le multi-arch reste conditionné à QEMU/binfmt sur les nœuds.

### Option C — Buildah

- `containers/buildah`, lit les Dockerfile, orienté rootless.
- **Pour** : maintenu, bonne ergonomie rootless.
- **Contre** : introduit une **seconde chaîne d'outils** sans bénéfice ici — les images
  ne seraient pas produites par le même moteur que les releases. Pertinent si
  l'écosystème était podman/RHEL ; ce n'est pas le cas.

## Décision

**Option retenue : B — BuildKit rootless.**

Le critère décisif n'est pas la maintenance — Buildah est maintenu autant — mais
**l'unicité du moteur** : `buildx` étant BuildKit, les deux voies deviennent deux clients
d'un même moteur, et l'équivalence des images cesse d'être une hypothèse. La mesure le
confirme : l'image produite in-cluster tombe à 0,8 Mio de celle produite par buildx, là où
Kaniko en produisait 7 % de plus.

Ce qu'on accepte de payer : `seccomp`/`AppArmor` en `Unconfined` sur le pod de build. C'est
strictement moins que le root exigé par Kaniko, et le pod ne porte ni jeton de
ServiceAccount ni privilège.

## Conséquences

- **Positives** : un seul moteur de build ; le pod ne tourne plus en root (uid 1000) ;
  le cache de couches est partagé avec les builds buildx (cache registre) ; le multi-arch
  devient atteignable in-cluster.
- **Négatives** : `seccomp`/`AppArmor` `Unconfined` requis, à vérifier si une politique
  d'admission stricte est introduite sur un cluster cible. Le point d'entrée a changé de
  nom (`build-kaniko.sh` → `build-incluster.sh`), nommé d'après l'intention et non le
  moteur pour qu'un futur changement ne renomme plus rien.
- **À surveiller** : le **multi-arch in-cluster n'est pas acquis** — construire une
  architecture étrangère aux nœuds exige QEMU/binfmt enregistré sur l'hôte (DaemonSet
  `tonistiigi/binfmt`). Le défaut reste `linux/amd64` et n'a **pas** été testé en arm64.
  Tant que binfmt n'est pas en place, les releases multi-arch restent le domaine de
  `build-k8s.sh` depuis un poste. Si un cluster cible impose PodSecurity `restricted`,
  la décision est à rouvrir.

## Suivi

- [x] Implémentation : PR #17 (`fix/qc-round2`), commit `f334588`
- [x] Build réel validé sur l'intégration (image `0.9.14-bk1`, commit `f334588`),
      manifeste relu dans le registre
- [x] Doc opérateur : [../../deploy/k8s/buildkit/README.md](../../deploy/k8s/buildkit/README.md)
- [ ] binfmt sur les nœuds, si le multi-arch in-cluster devient nécessaire
- [ ] Bascule des runners CI vers cette voie, si le besoin apparaît

Voir aussi [adr-0004](adr-0004-branches-experimentation.md) (fonctionnalité dont le
contrôle qualité a mis Kaniko au jour) et [adr-0002](adr-0002-proxy-llm-relais.md)
(principe de sécabilité).
