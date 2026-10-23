"""Victory domain — Lua builders and parsers."""

from __future__ import annotations

from civ_mcp.lua._helpers import _LUA_VICTORY_ENABLED, SENTINEL, _int
from civ_mcp.lua.models import (
    DemographicEntry,
    DominationCiv,
    SpaceProject,
    VictoryPlayerProgress,
    VictoryProgress,
)


def build_victory_proximity_query() -> str:
    """InGame: lightweight check for foreign victory threats. Safe for every turn."""
    return (
        """
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local relCount = {}
local relOwner = {}
local totalMajors = 0
-- 缺陷55(2026-08-09 败局根因):文化胜利临近从未被监控。美国 foreign
-- tourists 375 vs 全场门柱(希腊 domestic 381)只差 7 人时,任何简报都没
-- 报过一个字 —— T384 引擎通知弹出后 1 回合内他就赢了。第一遍先收
-- 每家 domestic(门柱),第二遍对每个已接触文明算 foreign 总量 vs 场上
-- 除它以外的最高 domestic,临门直接进 TurnEvent。
local alive = {}
local stay = {}
for i = 0, 62 do
    local p = Players[i]
    if p and p:IsMajor() and p:IsAlive() then
        alive[#alive + 1] = i
        local okS, s = pcall(function() return p:GetCulture():GetStaycationers() end)
        stay[i] = (okS and s) or 0
    end
end
for i = 0, 62 do
    local p = Players[i]
    if p and p:IsMajor() and p:IsAlive() then
        totalMajors = totalMajors + 1
        local majRel = p:GetReligion():GetReligionInMajorityOfCities()
        if majRel >= 0 then
            relCount[majRel] = (relCount[majRel] or 0) + 1
            if not relOwner[majRel] then
                local creator = -1
                for j = 0, 62 do
                    if Players[j] and Players[j]:IsAlive() and Players[j]:GetReligion():GetReligionTypeCreated() == majRel then creator = j end
                end
                if creator >= 0 and creator ~= me then
                    local rEntry = GameInfo.Religions[majRel]
                    local relName = rEntry and Locale.Lookup(rEntry.Name) or "Unknown"
                    if pDiplo:HasMet(creator) then
                        local cfg = PlayerConfigurations[creator]
                        relOwner[majRel] = Locale.Lookup(cfg:GetCivilizationShortDescription()) .. "|" .. relName
                    else
                        relOwner[majRel] = "Unknown civilization|" .. relName
                    end
                end
            end
        end
        if i ~= me and pDiplo:HasMet(i) then
            local cfg = PlayerConfigurations[i]
            local civName = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local okDvp, dvp = pcall(function() return p:GetStats():GetDiplomaticVictoryPoints() end)
            if okDvp and dvp and dvp >= 10 then
                print("DIPLO_THREAT|" .. civName .. "|" .. dvp)
            end
            local okSvp, svp = pcall(function() return p:GetStats():GetScienceVictoryPoints() end)
            if okSvp and svp and svp > 0 then
                local needed = 50
                pcall(function() needed = p:GetStats():GetScienceVictoryPointsTotalNeeded() end)
                print("SCI_THREAT|" .. civName .. "|" .. svp .. "|" .. needed)
            else
                -- Standard 规则集:GetScienceVictoryPoints 恒 0/不存在,上面的
                -- GS 哨兵永远沉默。退化用已建成太空港数当哨兵(能看到的
                -- 唯一硬信号;项目进度在战争迷雾里,引擎不导出)。
                local ports = 0
                pcall(function()
                    for _, city in p:GetCities():Members() do
                        for _, d in city:GetDistricts():Members() do
                            local dInfo = GameInfo.Districts[d:GetType()]
                            if dInfo and dInfo.DistrictType == "DISTRICT_SPACEPORT" and d:IsComplete() then
                                ports = ports + 1
                            end
                        end
                    end
                end)
                if ports >= 1 then
                    print("SCI_THREAT_STD|" .. civName .. "|" .. ports)
                end
            end
            -- 文化胜利:foreign tourists 总量 vs 场上除它外最高 domestic。
            local ft = 0
            local okF = pcall(function()
                local c = p:GetCulture()
                for _, oid in ipairs(alive) do
                    if oid ~= i then ft = ft + (c:GetTouristsFrom(oid) or 0) end
                end
            end)
            if okF then
                local gate, gateOwner = 0, ""
                for _, oid in ipairs(alive) do
                    if oid ~= i and stay[oid] and stay[oid] > gate then
                        gate = stay[oid]
                        local gcfg = PlayerConfigurations[oid]
                        gateOwner = (oid == me) and "us" or (pDiplo:HasMet(oid) and Locale.Lookup(gcfg:GetCivilizationShortDescription()) or "unmet civ")
                    end
                end
                print("CUL_THREAT|" .. civName .. "|" .. ft .. "|" .. gate .. "|" .. gateOwner)
            end
        end
    end
end
for relId, count in pairs(relCount) do
    if relOwner[relId] then
        print("REL_THREAT|" .. relOwner[relId] .. "|" .. count .. "|" .. totalMajors)
    end
end
"""
        + _LUA_VICTORY_ENABLED
        + """
print("{SENTINEL}")
"""
    ).replace("{SENTINEL}", SENTINEL)


def build_victory_progress_query() -> str:
    """Build a Lua query for victory progress of all players (InGame context).

    Outputs lines:
      PLAYER|pid|name|score|sciVP|sciNeeded|diploVP|tourism|milStr|techs|civics|relCities|staycationers|hasReligion
      CULTURE|civName|ourTourists|theirStaycationers|dominant
      CAPITAL|civName|holdsOwn
      DOMCIV|pid|met|name|holdsOwn   — ALL alive majors, met or not
      RELMAJ|civName|religionName
    """
    return f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pCul = Players[me]:GetCulture()

local dPop, dMil, dFood, dGold, dLand, dProd = {{}}, {{}}, {{}}, {{}}, {{}}, {{}}
for i = 0, 62 do
    local p = Players[i]
    if p and p:IsMajor() and p:IsAlive() then
        local met = pDiplo:HasMet(i) or i == me
        if met then
            local cfg = PlayerConfigurations[i]
            local name = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local st = p:GetStats()
            -- Victory-point APIs are Gathering Storm only; absent on the
            -- Standard ruleset, where these victories don't exist at all.
            local sciVP, sciNeeded, diploVP = 0, 0, 0
            pcall(function() sciVP = st:GetScienceVictoryPoints() end)
            pcall(function() sciNeeded = st:GetScienceVictoryPointsTotalNeeded() end)
            pcall(function() diploVP = st:GetDiplomaticVictoryPoints() end)
            local tourism = st:GetTourism()
            local milStr = st:GetMilitaryStrength()
            local techs = st:GetNumTechsResearched()
            local civics = st:GetNumCivicsCompleted()
            local relCities = st:GetNumCitiesFollowingReligion()
            local stay = p:GetCulture():GetStaycationers()
            local hasRel = p:GetReligion():GetReligionTypeCreated() >= 0
            local nCities = 0; for _ in p:GetCities():Members() do nCities = nCities + 1 end
            local pSci = p:GetTechs():GetScienceYield()
            local pCulYield = p:GetCulture():GetCultureYield()
            local pGold2 = p:GetTreasury():GetGoldYield() - p:GetTreasury():GetTotalMaintenance()
            -- Space race: count spaceports
            local spaceports = 0
            for _, city in p:GetCities():Members() do
                for _, d in city:GetDistricts():Members() do
                    local dInfo = GameInfo.Districts[d:GetType()]
                    if dInfo and dInfo.DistrictType == "DISTRICT_SPACEPORT" and d:IsComplete() then
                        spaceports = spaceports + 1
                    end
                end
            end
            print("PLAYER|" .. i .. "|" .. name .. "|" .. p:GetScore() .. "|" .. sciVP .. "|" .. sciNeeded .. "|" .. diploVP .. "|" .. tourism .. "|" .. milStr .. "|" .. techs .. "|" .. civics .. "|" .. relCities .. "|" .. stay .. "|" .. tostring(hasRel) .. "|" .. nCities .. "|" .. string.format("%.1f", pSci) .. "|" .. string.format("%.1f", pCulYield) .. "|" .. string.format("%.1f", pGold2))
            print("SPACE|" .. name .. "|" .. spaceports .. "|" .. sciVP .. "/" .. sciNeeded)
            if i == me then
                -- Space project chain detail (our player only).
                -- 缺陷53(2026-08-09):两处叠加把健康的 2/5 链渲染成"未开始"。
                -- ① 链表探测用了不存在的 PROJECT_LAUNCH_MARS_HABITAT(本局实名
                --   MARS_HABITATION),落进 MARS_COLONY 假链;② completed 判定
                --   依赖 sciVP,而 GetScienceVictoryPoints 是 GS 专属 API,
                --   Standard 规则集恒 0 —— 卫星/登月完成了也标不上。
                local spaceChain = {{}}
                if GameInfo.Projects["PROJECT_LAUNCH_MARS_HABITATION"] then
                    spaceChain = {{
                        {{"PROJECT_LAUNCH_EARTH_SATELLITE", "TECH_ROCKETRY", 1}},
                        {{"PROJECT_LAUNCH_MOON_LANDING", "TECH_SATELLITES", 2}},
                        {{"PROJECT_LAUNCH_MARS_HYDROPONICS", "TECH_NUCLEAR_FUSION", 3}},
                        {{"PROJECT_LAUNCH_MARS_HABITATION", "TECH_NUCLEAR_FUSION", 3}},
                        {{"PROJECT_LAUNCH_MARS_REACTOR", "TECH_NUCLEAR_FUSION", 3}},
                    }}
                elseif GameInfo.Projects["PROJECT_LAUNCH_MARS_HABITAT"] then
                    spaceChain = {{
                        {{"PROJECT_LAUNCH_EARTH_SATELLITE", "TECH_ROCKETRY", 1}},
                        {{"PROJECT_LAUNCH_MOON_LANDING", "TECH_SATELLITES", 2}},
                        {{"PROJECT_LAUNCH_MARS_HABITAT", "TECH_NUCLEAR_FUSION", 3}},
                        {{"PROJECT_LAUNCH_MARS_HYDROPONICS", "TECH_NUCLEAR_FUSION", 3}},
                        {{"PROJECT_LAUNCH_MARS_REACTOR", "TECH_NUCLEAR_FUSION", 3}},
                    }}
                else
                    spaceChain = {{
                        {{"PROJECT_LAUNCH_EARTH_SATELLITE", "TECH_ROCKETRY", 1}},
                        {{"PROJECT_LAUNCH_MOON_LANDING", "TECH_SATELLITES", 2}},
                        {{"PROJECT_LAUNCH_MARS_COLONY", "TECH_NUCLEAR_FUSION", 3}},
                    }}
                end
                if GameInfo.Projects["PROJECT_LAUNCH_EXOPLANET_EXPEDITION"] then
                    table.insert(spaceChain, {{"PROJECT_LAUNCH_EXOPLANET_EXPEDITION", "TECH_OFFWORLD_MISSION", 4}})
                end
                local pTechs = p:GetTechs()
                -- 第一遍:采集每件的原始信号(在造/可造/有科技)。
                local sig = {{}}
                for si, sp in ipairs(spaceChain) do
                    local projType, techType, tier = sp[1], sp[2], sp[3]
                    local projRow = GameInfo.Projects[projType]
                    if projRow then
                        local techRow = GameInfo.Technologies[techType]
                        local hasTech = techRow and pTechs:HasTech(techRow.Index) or false
                        local building, canBuild = false, false
                        local progPct, turnsLeft, cost, cityBuild = 0, 0, 0, ""
                        for _, city in p:GetCities():Members() do
                            local bq = city:GetBuildQueue()
                            local okH, h = pcall(function() return bq:GetCurrentProductionTypeHash() end)
                            if okH and h and h == projRow.Hash then
                                building = true
                                pcall(function()
                                    turnsLeft = bq:GetTurnsLeft(projRow.Hash)
                                    cost = math.floor(bq:GetProductionCost(projRow.Hash))
                                    local prog = bq:GetProductionProgress(projRow.Hash)
                                    if cost > 0 then progPct = math.floor(prog * 100 / cost) end
                                end)
                                cityBuild = Locale.Lookup(city:GetName())
                                break
                            end
                        end
                        if not building then
                            pcall(function()
                                for _, city in p:GetCities():Members() do
                                    local c = city:GetBuildQueue():GetProductionCost(projRow.Hash)
                                    if c and c > 0 then cost = math.floor(c); canBuild = true; break end
                                end
                            end)
                        end
                        -- 时序保护(缺陷112 的正解):本回合刚研究完的科技,引擎
                        -- 可能还没刷新可造列表,此时 canBuild=false 是【尚未结算】
                        -- 而不是【已完成】。T360 的 ~4/5 误报就出在这一帧。
                        -- 判据:该科技是不是"上一个完成的研究"。
                        local techJustIn = false
                        pcall(function()
                            if techRow and pTechs.GetLastResearchCompleted then
                                techJustIn = (pTechs:GetLastResearchCompleted() == techRow.Index)
                            end
                        end)
                        sig[si] = {{proj=projRow, projType=projType, techType=techType, tier=tier,
                                   hasTech=hasTech, building=building, canBuild=canBuild,
                                   techJustIn=techJustIn,
                                   progPct=progPct, turnsLeft=turnsLeft, cost=cost, cityBuild=cityBuild}}
                    end
                end
                -- 第二遍:定完成态。GS(sciNeeded>0)信 sciVP;Standard 用链级
                -- 推断 —— 高层级任何一件在造/可造,则低层级全部必然已完成
                -- (引擎强制前置)。这一条是硬规则,可以信。
                --
                -- 2026-08-12 缺陷112:同层曾经还有一条推断 ——「有科技 + 既不在造
                -- 也不可造 => 已完成」。它在 T360 报出 ~4/5,把刚刚才解锁的火星
                -- 反应堆和溶液栽培双双标成 [DONE]。当回合实测:拉文那的可造列表里
                -- 三件火星项目【全部在】,各 1800 产能,一件都没做。
                --
                -- 根因是 canBuild 被读得太早:核聚变就在这一回合研究完,快照跑的
                -- 那一帧引擎还没刷新可造列表,GetProductionCost 返回 0 → canBuild
                -- 为假 → 判定"完成"。和缺陷108 同族(把异步的"尚未结算"读成结论)。
                --
                -- 当时的处理是【整条删掉】,理由写的是"少报只是保守,多报会让人
                -- 以为胜利在望"。T455 推翻了这句话:五件太空项目全部完成、科学
                -- 胜利已经达成,而这里报的是 0/5 全部 unlocked —— 少报把【已经
                -- 赢了】显示成"一件没做",我据此把胜利类型也认错了。少报和多报
                -- 一样有害,只是错的方向不同。
                --
                -- 正确的修法本来就摆在原因里:T360 的误判是【科技刚到手那一帧
                -- 引擎还没刷新可造列表】,是时序问题,不是判据问题。所以恢复这条
                -- 推断,加上时序保护:只有当该项目的前置科技【不是本回合刚到手】
                -- 时才敢用"不可造"反推完成。techJustIn 由下面的 justResearched
                -- 判定 —— 本回合完成的科技一律不参与推断,等下一回合引擎刷新完
                -- 再说。宁可晚一回合,不要错一整局。
                local reachedTier = 0
                for _, s in pairs(sig) do
                    if (s.building or s.canBuild) and s.tier > reachedTier then reachedTier = s.tier end
                end
                local completedCount = 0
                for si, sp in ipairs(spaceChain) do
                    local s = sig[si]
                    if s then
                        local status = "locked"
                        if s.building then
                            status = "building"
                        elseif sciNeeded > 0 then
                            if completedCount < sciVP then
                                status = "completed"; completedCount = completedCount + 1
                            elseif s.hasTech then
                                status = s.canBuild and "ready" or "unlocked"
                            end
                        else
                            if s.tier < reachedTier then
                                status = "completed"; completedCount = completedCount + 1
                            elseif s.hasTech and not s.canBuild and not s.techJustIn then
                                -- 有科技、不在造、也不可造,而且这科技不是本回合
                                -- 刚到手 —— 项目完成后就会从可造列表消失,这是
                                -- 它唯一的指纹。时序保护(techJustIn)挡住的正是
                                -- T360 那种"刚解锁还没刷新"的假完成。
                                status = "completed"; completedCount = completedCount + 1
                            elseif s.hasTech then
                                status = s.canBuild and "ready" or "unlocked"
                            end
                        end
                        print("SPACEPROJ|" .. s.projType .. "|" .. Locale.Lookup(s.proj.Name) .. "|" .. status .. "|" .. s.progPct .. "|" .. s.turnsLeft .. "|" .. s.cost .. "|" .. s.techType .. "|" .. tostring(s.hasTech) .. "|" .. s.cityBuild)
                    end
                end
                print("SPACEDONE|" .. completedCount .. "|" .. #spaceChain)
            end
            if i ~= me then
                local ourTourists = pCul:GetTouristsFrom(i)
                local theirStay = p:GetCulture():GetStaycationers()
                local dominant = pCul:IsDominantOver(i)
                print("CULTURE|" .. name .. "|" .. ourTourists .. "|" .. theirStay .. "|" .. tostring(dominant))
                -- 缺陷55:对手的文化胜利临近度(foreign 总量 vs 场上最后门柱)。
                local ft = 0
                local okF = pcall(function()
                    local c = p:GetCulture()
                    for j = 0, 62 do
                        local o = Players[j]
                        if j ~= i and o and o:IsMajor() and o:IsAlive() then
                            ft = ft + (c:GetTouristsFrom(j) or 0)
                        end
                    end
                end)
                if okF then
                    local gate, gateOwner = 0, ""
                    for j = 0, 62 do
                        local o = Players[j]
                        if j ~= i and o and o:IsMajor() and o:IsAlive() then
                            local okS2, s2 = pcall(function() return o:GetCulture():GetStaycationers() end)
                            if okS2 and s2 and s2 > gate then
                                gate = s2
                                if j == me then gateOwner = "us"
                                elseif pDiplo:HasMet(j) then
                                    local gcfg = PlayerConfigurations[j]
                                    gateOwner = Locale.Lookup(gcfg:GetCivilizationShortDescription())
                                else gateOwner = "unmet civ" end
                            end
                        end
                    end
                    print("CULTHREAT|" .. name .. "|" .. ft .. "|" .. gate .. "|" .. gateOwner)
                end
            end
            local cap = p:GetCities():GetCapitalCity()
            local holdsOwn = cap and cap:IsOriginalCapital() or false
            print("CAPITAL|" .. name .. "|" .. tostring(holdsOwn))
            local majRel = p:GetReligion():GetReligionInMajorityOfCities()
            local relName = "none"
            if majRel >= 0 then
                local r = GameInfo.Religions[majRel]
                if r then relName = r.ReligionType end
            end
            print("RELMAJ|" .. name .. "|" .. relName)
            local createdRel = p:GetReligion():GetReligionTypeCreated()
            if createdRel >= 0 then
                local rEntry = GameInfo.Religions[createdRel]
                local fName = rEntry and Locale.Lookup(rEntry.Name) or "Unknown"
                print("RELFOUNDED|" .. name .. "|" .. fName)
            end
        end
        -- Demographics: collect for ALL alive majors (anonymized aggregates)
        local totalPop = 0
        local totalFood, totalProd = 0, 0
        local ownedPlots = 0
        for _, c in p:GetCities():Members() do
            totalPop = totalPop + c:GetPopulation()
            totalFood = totalFood + c:GetYield(0)
            totalProd = totalProd + c:GetYield(1)
            local ok, pp = pcall(function() return Map.GetCityPlots():GetPurchasedPlots(c) end)
            if ok and pp then ownedPlots = ownedPlots + #pp end
        end
        local milVal = p:GetStats():GetMilitaryStrength()
        local netGold = p:GetTreasury():GetGoldYield() - p:GetTreasury():GetTotalMaintenance()
        table.insert(dPop, {{v=totalPop, mine=(i==me)}})
        table.insert(dMil, {{v=milVal, mine=(i==me)}})
        table.insert(dFood, {{v=totalFood, mine=(i==me)}})
        table.insert(dGold, {{v=netGold, mine=(i==me)}})
        table.insert(dLand, {{v=ownedPlots, mine=(i==me)}})
        table.insert(dProd, {{v=totalProd, mine=(i==me)}})
    end
end
local function emitDemo(label, arr)
    table.sort(arr, function(a, b) return a.v > b.v end)
    local best, worst, total = arr[1].v, arr[#arr].v, 0
    local myRank, myVal = 0, 0
    for idx, e in ipairs(arr) do
        total = total + e.v
        if e.mine then myRank = idx; myVal = e.v end
    end
    local avg = total / #arr
    print("DEMO|" .. label .. "|" .. myRank .. "|" .. string.format("%.1f", myVal) .. "|" .. string.format("%.1f", best) .. "|" .. string.format("%.1f", avg) .. "|" .. string.format("%.1f", worst))
end
emitDemo("Population", dPop)
emitDemo("Soldiers", dMil)
emitDemo("CropYield", dFood)
emitDemo("GNP", dGold)
emitDemo("Land", dLand)
emitDemo("Goods", dProd)
-- 统治胜利:必须枚举【全部】存活大文明,不能只算已接触的。
-- 2026-08-08 T190 实测缺陷:上面整段 PLAYER/CAPITAL 查询被 `if met` 过滤,
-- 本局 6 个大文明里希腊、刚果从未接触,统治进度就只报出 4 个,把
-- 「还差 4 座首都(2 座在未探索大陆)」显示成「还差 2 座」——评估给了
-- Domination 40% 且写"2 known capitals to capture",读起来像唾手可得,
-- 于是从 T158 一路打到 T190 没再复核路径。未接触文明的首都归属对我
-- 不可见,如实标 unknown,但【必须计入分母】。
local nMajors = 0
local nRels = 0
for i = 0, 62 do
    local p = Players[i]
    if p and p:IsMajor() and p:IsAlive() then
        nMajors = nMajors + 1
        if p:GetReligion():GetReligionTypeCreated() >= 0 then nRels = nRels + 1 end
        local met = pDiplo:HasMet(i) or i == me
        local nm = "UNKNOWN"
        local holds = "unknown"
        if met then
            local cfg = PlayerConfigurations[i]
            nm = Locale.Lookup(cfg:GetCivilizationShortDescription())
            local cap = p:GetCities():GetCapitalCity()
            local ok, v = pcall(function() return cap and cap:IsOriginalCapital() end)
            holds = (ok and v) and "true" or "false"
        end
        print("DOMCIV|" .. i .. "|" .. tostring(met) .. "|" .. nm .. "|" .. holds)
    end
end
print("RELSLOTS|" .. nRels .. "|" .. (math.floor(nMajors / 2) + 1))
{_LUA_VICTORY_ENABLED}
print("{SENTINEL}")
"""


def parse_victory_progress_response(lines: list[str]) -> VictoryProgress:
    """Parse victory progress from Lua output."""
    players: list[VictoryPlayerProgress] = []
    our_tourists: dict[str, int] = {}
    their_stay: dict[str, int] = {}
    capitals: dict[str, bool] = {}
    dom_civs: list[DominationCiv] = []
    rel_majority: dict[str, str] = {}
    rel_founded_names: dict[str, str] = {}
    religions_founded = 0
    religions_max = 0
    demographics: dict[str, DemographicEntry] = {}
    space_projects: list[SpaceProject] = []
    enabled_victories: set[str] = set()
    culture_threats: dict[str, tuple[int, int, str]] = {}
    space_completed = -1
    space_chain_total = 0

    for line in lines:
        if line.startswith("PLAYER|"):
            p = line.split("|")
            if len(p) < 14:
                continue
            players.append(
                VictoryPlayerProgress(
                    player_id=int(p[1]),
                    name=p[2],
                    score=_int(p[3]),
                    science_vp=_int(p[4]),
                    science_vp_needed=_int(p[5]),
                    diplomatic_vp=_int(p[6]),
                    tourism=_int(p[7]),
                    military_strength=_int(p[8]),
                    techs_researched=_int(p[9]),
                    civics_completed=_int(p[10]),
                    religion_cities=_int(p[11]),
                    staycationers=_int(p[12]),
                    has_religion=p[13] == "true",
                    num_cities=_int(p[14]) if len(p) > 14 else 0,
                    science_yield=float(p[15]) if len(p) > 15 else 0.0,
                    culture_yield=float(p[16]) if len(p) > 16 else 0.0,
                    gold_yield=float(p[17]) if len(p) > 17 else 0.0,
                )
            )
        elif line.startswith("SPACE|"):
            p = line.split("|")
            if len(p) >= 4:
                sp_name = p[1]
                sp_ports = int(p[2])
                sp_prog = p[3]
                for pl in players:
                    if pl.name == sp_name:
                        pl.spaceports = sp_ports
                        pl.space_progress = sp_prog
                        break
        elif line.startswith("CULTURE|"):
            p = line.split("|")
            if len(p) >= 5:
                our_tourists[p[1]] = int(p[2])
                their_stay[p[1]] = int(p[3])
        elif line.startswith("CULTHREAT|"):
            p = line.split("|")
            if len(p) >= 5:
                culture_threats[p[1]] = (_int(p[2]), _int(p[3]), p[4])
        elif line.startswith("SPACEDONE|"):
            p = line.split("|")
            if len(p) >= 3:
                space_completed = _int(p[1])
                space_chain_total = _int(p[2])
        elif line.startswith("CAPITAL|"):
            p = line.split("|")
            if len(p) >= 3:
                capitals[p[1]] = p[2] == "true"
        elif line.startswith("DOMCIV|"):
            p = line.split("|")
            if len(p) >= 5:
                met = p[2] == "true"
                dom_civs.append(
                    DominationCiv(
                        player_id=_int(p[1]),
                        met=met,
                        name=p[3],
                        holds_own_capital=(p[4] == "true") if met else None,
                    )
                )
        elif line.startswith("RELMAJ|"):
            p = line.split("|")
            if len(p) >= 3:
                rel_majority[p[1]] = p[2]
        elif line.startswith("RELFOUNDED|"):
            p = line.split("|")
            if len(p) >= 3:
                rel_founded_names[p[1]] = p[2]
        elif line.startswith("RELSLOTS|"):
            p = line.split("|")
            if len(p) >= 3:
                religions_founded = int(p[1])
                religions_max = int(p[2])
        elif line.startswith("SPACEPROJ|"):
            p = line.split("|")
            if len(p) >= 9:
                space_projects.append(
                    SpaceProject(
                        project_type=p[1],
                        name=p[2],
                        status=p[3],
                        progress_pct=int(p[4]),
                        turns_remaining=int(p[5]),
                        cost=int(p[6]),
                        tech_prereq=p[7],
                        has_tech=p[8] == "true",
                        city_name=p[9] if len(p) > 9 else "",
                    )
                )
        elif line.startswith("DEMO|"):
            p = line.split("|")
            if len(p) >= 6:
                demographics[p[1]] = DemographicEntry(
                    rank=int(p[2]),
                    value=float(p[3]),
                    best=float(p[4]),
                    average=float(p[5]),
                    worst=float(p[6]) if len(p) > 6 else float(p[5]),
                )
        elif line.startswith("VENABLED|"):
            enabled_victories.add(line.split("|", 1)[1])

    return VictoryProgress(
        players=players,
        our_tourists_from=our_tourists,
        their_staycationers=their_stay,
        capitals_held=capitals,
        domination_civs=dom_civs,
        religion_majority=rel_majority,
        religion_founded_names=rel_founded_names,
        religions_founded=religions_founded,
        religions_max=religions_max,
        demographics=demographics,
        space_projects=space_projects,
        enabled_victories=enabled_victories,
        culture_threats=culture_threats,
        space_completed=space_completed,
        space_chain_total=space_chain_total,
    )
