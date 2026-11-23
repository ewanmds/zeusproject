# Classement Auto Outcome et compteur REST partagé

Le scanner classe les carnets admissibles par `volume_usd_1m` décroissant :
la somme des dollars de trades publics observés sur les 60 dernières secondes.
La file estimée départage les volumes identiques. Le tableau trie avant de
retenir ses huit lignes et indique le tri décroissant sur USD/min ; un clic
sur les colonnes permet de changer le tri d'affichage.

Ce débit public n'est pas le volume exécuté par le bot. Il ne garantit ni
remplissage, ni résultat financier. Un marché à zéro dollar observé reste
admissible si les contrôles de carnet, profondeur, spread, calendrier et
fraîcheur passent. Aucun seuil minimum de flux ni attente de 60 secondes
n'est réintroduit.

Les 16 premières places de lecture des carnets privilégient le volume observé.
Les autres places explorent le catalogue, dans une limite de 24 carnets. Un
nouveau marché à fort débit entre sans attendre la prochaine rotation de
découverte. Les carnets WebSocket restent prioritaires sur les lectures REST.

La rotation attend toujours une position nulle et un candidat stable pendant
30 secondes. Elle exige au moins 25 % de USD/min public en plus, en comparant
le débit actuel du marché en cours lorsqu'il est disponible, puis celui de
la sélection initiale à défaut. Le temps estimé de file ne remplace plus ce
critère. Les budgets d'ordres, de position et de perte sont conservés.

Le compteur `read-budget.sqlite3` sérialise désormais ses ouvertures et
transactions par un verrou de processus et utilise WAL. Les transactions
restent courtes, sans réseau ni attente de reprise à l'intérieur. Une
contention persistante produit une erreur de lecture typée 503 avec un délai
de reprise ; aucune requête HTTP ne part sans réservation confirmée. Seules
les lectures idempotentes utilisent leur politique de reprise existante.
Les envois d'ordres signés ne sont pas rejoués. Un arrêt intervenu pendant
l'attente du compteur empêche aussi la lecture HTTP.

Référence SQLite : [WAL et concurrence](https://www.sqlite.org/wal.html).
Aucune suppression de base ni migration des données de trading n'est requise.
Le nouveau code est actif après la mise à jour et le redémarrage du workspace.
