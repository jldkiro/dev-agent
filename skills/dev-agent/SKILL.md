---
name: dev-agent
description: Déporter les tests, builds et commandes de développement lourdes sur un worker GitHub éphémère, isolé par session, pour économiser le CPU et la RAM du poste. Utiliser pour l'exécution distante; pas pour un déploiement, un service permanent ou une sauvegarde.
---

# Exécuter sur le worker éphémère de la session

L'outil `dev-agent` est installé dans le PATH du poste. L'agent, le fil T3 et
l'accès au modèle restent locaux. Chaque `CODEX_THREAD_ID` reçoit son propre
worker; ne pas remplacer cet identifiant pour réutiliser celui d'un autre fil.

Choisir le profil explicitement : `ha-mcp` pour le développement public de
ha-mcp, `private` pour un travail privé, `public` pour qualifier l'outillage public.
Pour ha-mcp, tests, builds, lint, installations de dépendances et scripts de
développement passent obligatoirement par ce wrapper. Aucun secours local
automatique si le worker échoue. Lectures, éditions et Git léger restent locaux.

```text
dev-agent --profile ha-mcp run --project CHEMIN_LOCAL_DU_PROJET -- uv run pytest
```

Le wrapper démarre ou réutilise le worker, transmet les fichiers locaux utiles
(y compris non committés), puis conserve les sorties et le code de retour.
Les éditions restent locales. Ne pas transmettre le dossier personnel, les
credentials ou le dépôt de contexte ambiant pour une commande d'un autre projet.
Les exclusions automatiques ne remplacent pas le choix des fichiers nécessaires.

Pour une commande longue, ajouter `--detach` avant `--`, conserver le job retourné,
puis utiliser `dev-agent --profile PROFIL wait JOB` ou `status --job JOB` avec
le même profil. Annuler avec `dev-agent --profile PROFIL cancel JOB`.
Après une réponse réseau incertaine, retrouver ce job;
ne pas répéter la commande ni l'envoyer à un nouveau worker automatiquement.

**La VM et les processus restent jetables.** Expiration après une heure
d'inactivité pour les profils publics et cinq minutes pour le privé, sans
commande active ni transfert. Les sondes de statut/logs ne prolongent pas ce délai.
Le profil privé sauvegarde ses fichiers admissibles dans deux checkpoints Git
indépendants et restaure automatiquement ceux de la même session au redémarrage.
Une panne ou annulation forcée peut empêcher la sauvegarde. Les profils publics
ne sauvegardent ni ne restaurent automatiquement. Récupérer les résultats utiles :

```text
dev-agent --profile ha-mcp get --job JOB chemin/relatif/resultat.json CHEMIN_LOCAL
dev-agent --profile ha-mcp stop
```

Une commande reçoit un snapshot neuf du projet. Ne pas supposer que les fichiers
produits par une autre commande figurent dans ce snapshot; récupérer les résultats
localement ou choisir une seule commande qui réalise l'enchaînement voulu.
Un snapshot identique évite un nouvel upload, mais produit quand même un nouvel
espace de travail. Ce cache appartient au worker courant et disparaît avec lui.
En privé, les modifications distantes sont réconciliées avec les nouveaux
fichiers locaux; un conflit est signalé sans démarrer la commande. Attendre la
fin d'une commande avant de synchroniser de nouveau le même projet privé.
Les anciens jobs/logs/processus ne sont pas restaurés; relire les fichiers par
une nouvelle commande. Si la sauvegarde précédente n'est pas confirmée, demander
une décision avant d'utiliser `--accept-stale-checkpoint` pour accepter une reprise
potentiellement ancienne. Ne jamais changer l'identité de session pour contourner
une erreur ou reprendre automatiquement les fichiers d'un autre fil.
Le cache uv peut être réutilisé pendant la vie du worker. Une commande active
survit à la déconnexion SSH; elle reste limitée par la durée maximale du job.
Un conteneur lancé avec `docker run -d` ne compte pas, à lui seul, comme une
commande encore active : garder un travail géré au premier plan si sa durée
doit protéger la session, sans ajouter de keepalive artificiel.

`stop` refuse les commandes actives; `stop --force` signifie les abandonner
explicitement. Aucun jeton Cloudflare, T3 ou fournisseur LLM n'est nécessaire sur
le worker. L'authentification GitHub pour allouer le compute reste sur le client.
Le superviseur privé utilise le jeton GitHub éphémère du job pour les checkpoints;
ce jeton n'entre ni dans l'environnement des commandes ni dans leurs archives.

Les sorties métier restent sur le worker et passent par SSH; les logs Actions
ne donnent que le cycle de vie et les métadonnées publiques. Lire les sorties
avec `dev-agent logs JOB` ou `wait JOB`, puis récupérer les résultats utiles.
