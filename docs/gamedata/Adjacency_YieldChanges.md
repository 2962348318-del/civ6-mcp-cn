# Adjacency_YieldChanges

> 相邻加成的具体规则 — 区域放置算法的依据

共 45 行。

| ID | YieldType | YieldChange | TilesRequired | AdjacentResource | AdjacentRiver | AdjacentDistrict | AdjacentTerrain | AdjacentFeature | AdjacentWonder | OtherDistrictAdjacent |
|---|---|---|---|---|---|---|---|---|---|---|
| Mountains_Science1 | YIELD_SCIENCE | 1 | 1 | false | false |  | TERRAIN_GRASS_MOUNTAIN |  | false | false |
| Mountains_Science2 | YIELD_SCIENCE | 1 | 1 | false | false |  | TERRAIN_PLAINS_MOUNTAIN |  | false | false |
| Mountains_Science3 | YIELD_SCIENCE | 1 | 1 | false | false |  | TERRAIN_DESERT_MOUNTAIN |  | false | false |
| Mountains_Science4 | YIELD_SCIENCE | 1 | 1 | false | false |  | TERRAIN_TUNDRA_MOUNTAIN |  | false | false |
| Mountains_Science5 | YIELD_SCIENCE | 1 | 1 | false | false |  | TERRAIN_SNOW_MOUNTAIN |  | false | false |
| Jungle_Science | YIELD_SCIENCE | 1 | 2 | false | false |  |  | FEATURE_JUNGLE | false | false |
| District_Science | YIELD_SCIENCE | 1 | 2 | false | false |  |  |  | false | true |
| NaturalWonder_Faith | YIELD_FAITH | 2 | 1 | false | false |  |  |  | false | false |
| Mountain_Faith1 | YIELD_FAITH | 1 | 1 | false | false |  | TERRAIN_GRASS_MOUNTAIN |  | false | false |
| Mountain_Faith2 | YIELD_FAITH | 1 | 1 | false | false |  | TERRAIN_PLAINS_MOUNTAIN |  | false | false |
| Mountain_Faith3 | YIELD_FAITH | 1 | 1 | false | false |  | TERRAIN_DESERT_MOUNTAIN |  | false | false |
| Mountain_Faith4 | YIELD_FAITH | 1 | 1 | false | false |  | TERRAIN_TUNDRA_MOUNTAIN |  | false | false |
| Mountain_Faith5 | YIELD_FAITH | 1 | 1 | false | false |  | TERRAIN_SNOW_MOUNTAIN |  | false | false |
| Forest_Faith | YIELD_FAITH | 1 | 2 | false | false |  |  | FEATURE_FOREST | false | false |
| District_Faith | YIELD_FAITH | 1 | 2 | false | false |  |  |  | false | true |
| River_Gold | YIELD_GOLD | 2 | 1 | false | true |  |  |  | false | false |
| Harbor_Gold | YIELD_GOLD | 2 | 1 | false | false | DISTRICT_HARBOR |  |  | false | false |
| RoyalDock_Gold | YIELD_GOLD | 2 | 1 | false | false | DISTRICT_ROYAL_NAVY_DOCKYARD |  |  | false | false |
| District_Gold | YIELD_GOLD | 1 | 2 | false | false |  |  |  | false | true |
| SeaResource_Gold | YIELD_GOLD | 1 | 1 | false | false |  |  |  | false | false |
| Wonder_Culture | YIELD_CULTURE | 1 | 1 | false | false |  |  |  | true | false |
| District_Culture | YIELD_CULTURE | 1 | 2 | false | false |  |  |  | false | true |
| District_Culture_Standard | YIELD_CULTURE | 1 | 1 | false | false |  |  |  | false | true |
| District_Culture_City_Center | YIELD_CULTURE | 1 | 1 | false | false | DISTRICT_CITY_CENTER |  |  | false | false |
| Mine_Production | YIELD_PRODUCTION | 1 | 1 | false | false |  |  |  | false | false |
| Quarry_Production | YIELD_PRODUCTION | 1 | 1 | false | false |  |  |  | false | false |
| District_Production | YIELD_PRODUCTION | 1 | 2 | false | false |  |  |  | false | true |
| Commerical_Hub_Production | YIELD_PRODUCTION | 2 | 1 | false | false | DISTRICT_COMMERCIAL_HUB |  |  | false | false |
| Resource_Production | YIELD_PRODUCTION | 1 | 1 | true | false |  |  |  | false | false |
| Harbor_City_Gold | YIELD_GOLD | 2 | 1 | false | false | DISTRICT_CITY_CENTER |  |  | false | false |
| RoyalDock_City_Gold | YIELD_GOLD | 2 | 1 | false | false | DISTRICT_CITY_CENTER |  |  | false | false |
| GBR_Science | YIELD_SCIENCE | 2 | 1 | false | false |  |  | FEATURE_BARRIER_REEF | false | false |
| Farms_MedievalAdjacency | YIELD_FOOD | 1 | 2 | false | false |  |  |  | false | false |
| Farms_MechanizedAdjacency | YIELD_FOOD | 1 | 1 | false | false |  |  |  | false | false |
| GreatWall_Gold | YIELD_GOLD | 1 | 1 | false | false |  |  |  | false | false |
| GreatWall_Culture | YIELD_CULTURE | 1 | 1 | false | false |  |  |  | false | false |
| Kurgan_Faith | YIELD_FAITH | 1 | 1 | false | false |  |  |  | false | false |
| Kurgan_Faith_Stirrups | YIELD_FAITH | 2 | 1 | false | false |  |  |  | false | false |
| ColossalHead_FaithForest | YIELD_FAITH | 1 | 2 | false | false |  |  | FEATURE_FOREST | false | false |
| ColossalHead_FaithJungle | YIELD_FAITH | 1 | 2 | false | false |  |  | FEATURE_JUNGLE | false | false |
| Mission_Science_HolySite | YIELD_SCIENCE | 1 | 1 | false | false | DISTRICT_HOLY_SITE |  |  | false | false |
| Mission_Science_Campus | YIELD_SCIENCE | 1 | 1 | false | false | DISTRICT_CAMPUS |  |  | false | false |
| Chateau_WonderEarly | YIELD_CULTURE | 1 | 1 | false | false |  |  |  | true | false |
| Chateau_WonderLate | YIELD_CULTURE | 2 | 1 | false | false |  |  |  | true | false |
| Chateau_River | YIELD_GOLD | 2 | 1 | false | true |  |  |  | false | false |