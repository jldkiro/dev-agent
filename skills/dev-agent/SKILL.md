---
name: dev-agent
description: Déporter les tests, builds et commandes de développement lourdes sur un worker GitHub éphémère, isolé par session, pour économiser le CPU et la RAM du poste. Utiliser pour l'exécution distante; pas pour un déploiement, un service permanent ou une sauvegarde.
---

# Exécuter sur le worker éphémère de la session

L'outil `dev-agent` est installé dans le PATH du poste. L'agent, le fil T3 et
l'accès au modèle restent locaux. Chaque `CODEX_THREAD_ID` reçoit son propre
worker; ne pas remplacer cet identifiant pour réutiliser celui d'un autre fil.

Utiliser pour les commandes lourdes dont le projet explicite est connu :

```text
dev-agent run --project CHEMIN_LOCAL_DU_PROJET -- uv run pytest
```

Le wrapper démarre ou réutilise le worker, transmet les fichiers locaux utiles
(y compris non committés), puis conserve les sorties et le code de retour.
Les éditions restent locales. Ne pas transmettre le dossier personnel, les
credentials ou le dépôt de contexte ambiant pour une commande d'un autre projet.
Les exclusions automatiques ne remplacent pas le choix des fichiers nécessaires.

Pour une commande longue, ajouter `--detach` avant `--`, conserver le job retourné,
puis utiliser `dev-agent wait JOB` ou `dev-agent status --job JOB`. Annuler avec
`dev-agent cancel JOB`. Après une réponse réseau incertaine, retrouver ce job;
ne pas répéter la commande ni l'envoyer à un nouveau worker automatiquement.

**Tout état distant est jetable.** Le worker disparaît après quinze minutes sans
activité réelle et sans commande active. Les sondes de statut ne le gardent pas
en vie. Une nouvelle allocation ne restaure pas ses fichiers. Récupérer les
résultats utiles avant de conclure :

```text
dev-agent get --job JOB chemin/relatif/resultat.json CHEMIN_LOCAL
dev-agent stop
```

Une commande reçoit un snapshot neuf du projet. Ne pas supposer que les fichiers
produits par une autre commande figurent dans ce snapshot; récupérer les résultats
localement ou choisir une seule commande qui réalise l'enchaînement voulu.
Un snapshot identique évite un nouvel upload, mais produit quand même un nouvel
espace de travail. Ce cache appartient au worker courant et disparaît avec lui.
Le cache uv peut être réutilisé pendant la vie du worker. Une commande active
survit à la déconnexion SSH; elle reste limitée par la durée maximale du job.
Un conteneur lancé avec `docker run -d` ne compte pas, à lui seul, comme une
commande encore active : garder un travail géré au premier plan si sa durée
doit protéger la session, sans ajouter de keepalive artificiel.

`stop` refuse les commandes actives; `stop --force` signifie les abandonner
explicitement. Aucun jeton Cloudflare, T3 ou fournisseur LLM n'est nécessaire sur
le worker. L'authentification GitHub pour allouer le compute reste sur le client.

Les sorties métier restent sur le worker et passent par SSH; les logs Actions
ne donnent que le cycle de vie et les métadonnées publiques. Lire les sorties
avec `dev-agent logs JOB` ou `wait JOB`, puis récupérer les résultats utiles.
