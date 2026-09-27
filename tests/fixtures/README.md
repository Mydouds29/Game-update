# Fixtures de test

Ces fichiers reproduisent la structure des sources réelles (réponse JSON de
`ISteamNews/GetNewsForApp/v2`, page de patch notes Blizzard News, liste
d'articles), mais ils ont été **reconstitués** : l'environnement de
développement initial n'avait pas accès à Steam ni à Blizzard.

Dès que le VPS est en place, enregistrer les vraies réponses :

    flask --app wsgi record-fixture palworld-steam
    flask --app wsgi record-fixture diablo-4-blizzard

puis ajouter des tests sur les fichiers `*.recorded.*` produits (et ajuster
les parseurs si la structure réelle diffère).
