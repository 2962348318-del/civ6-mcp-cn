"""L0 工具层拦截 —— 把致命错误在执行前挡下来。

设计依据(改造方案 铁律三):能用代码判定的,不交给模型判断。

「开拓者两格内有没有敌方军事单位」是一次坐标计算,不该让模型每回合自己
调 threat_scan 再心算距离——它一定会漏。2026-07-31 的 52 回合复盘里,
8 个单位死亡有 5 个是平民被蛮族杀死,全部发生在模型"已经知道附近有蛮族"
的情况下。知道 ≠ 会在下一次移动时想起来。

拦截不是禁止。每条规则返回:拒绝原因 + 当前局面 + 可选做法,
模型确认要冒险时可以传 force=True 显式绕过。

可见性遵循与 build_threat_scan_query 相同的规则:只统计
PlayersVisibility:IsVisible 为真的敌方单位,不泄露战争迷雾里的信息。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from civ_mcp.lua._helpers import SENTINEL

# 平民单位:防御力 0,被敌方军事单位贴上即被俘或被杀(蛮族直接杀死)
CIVILIAN = "FORMATION_CLASS_CIVILIAN"
RANGED = "PROMOTION_CLASS_RANGED"

# 远距攻击类兵种(攻击不吃反击)。2026-08-07 实机(苏美尔局 T173):
# 投石机对京都远程齐射被 STAGGERED_ASSAULT 拦下,而那条门禁的选项③
# 自己就写着"用远程打一发(零反伤)" —— 它把自己推荐的做法也拦了。
# 根因:is_ranged 只认 PROMOTION_CLASS_RANGED,投石机是 _SIEGE。
# 这正是 2026-08-04 攻击路径修过的判据(RangedCombat>0 或 Bombard>0),
# discipline 这套 MoveContext 没跟上 —— 又一例「同一个概念两套实现」。
# 顺带的正确性收益:RANGED_ALREADY_MOVED 门禁现在也覆盖攻城单位
# (攻城单位移动后同样不能攻击,之前它漏网)。
STANDOFF_CLASSES = frozenset(
    {
        RANGED,
        "PROMOTION_CLASS_SIEGE",
        "PROMOTION_CLASS_NAVAL_RANGED",
        "PROMOTION_CLASS_NAVAL_CARRIER",
        "PROMOTION_CLASS_AIR_BOMBER",
        "PROMOTION_CLASS_AIR_FIGHTER",
    }
)

# 敌军扫描窗口(格)。取常见最大威胁半径:骑乘 4 移动 + 远程 2 射程。
# 具体危险判定不用这个数 —— 用每个敌军自己的 moves + range(见 EnemySighting)。
SCAN_RADIUS = 6

# 敌军数据缺失时的保守兜底移动力(远古步兵水平)
FALLBACK_ENEMY_MOVES = 2

# 「援军在路上」的半径(格)。4 格 ≈ 远古单位 2 回合可达。
# 定义在这里而不是 discipline.py:Lua 查询要用它,而 safety 不能反向依赖
# discipline(discipline 依赖 safety 的 MoveContext/Verdict)。
REINFORCE_RADIUS = 4

# 陆路探测的展开上限(格)。必须够搜完【一整个大陆连通分量】—— 否则搜索
# 每次都被截断,永远给不出"确实没有陆路"的结论,整个门形同虚设。
#
# 2026-08-11 罗马局 T268 实测:从 (62,49) 找去伦敦那块地,搜完我方大陆用了
# 1603 格。初版上限设 400,于是这张图上的每一次探测都在 400 格处截断、
# 判"不确定"、放行 —— 门写好了却一次没生效过。保守的默认值在这里不是
# 安全,是静默失效。
#
# 4000 格能覆盖标准图上的整块大陆(本局全图 2914 格陆地)。实测这段跑完
# 没有可感延迟,它搭在移动前查询(约 324ms)上,不额外增加往返。
LAND_PROBE_LIMIT = 4000


@dataclass
class EnemySighting:
    """一个可见敌方军事单位,以及"假设我是它"的威胁推演。

    用户提出的关键想法(2026-07-31):不知道对面会干什么,就假设自己是对面。
    对蛮族这种确定性规则机,推演不是猜测而是计算 —— 它下一回合的可达范围
    = 移动力 + 射程,这是能力上界,不需要猜意图。

    初版用固定 2 格危险半径,而复盘 T33 抓死开拓者的是移动力 4 的骑手 ——
    固定半径根本拦不住。现在每个敌军按自己的机动力算。
    """

    x: int
    y: int
    dist: int  # 距离目标格
    unit_type: str
    cs: int  # 战力(近战/远程取大)
    owner: str
    moves: int = FALLBACK_ENEMY_MOVES
    range_: int = 0  # 射程(近战 0)

    @property
    def is_naval(self) -> bool:
        """海军单位。gamedata Domain,查不到时按类型名兜底。"""
        try:
            from civ_mcp import war_planner

            for r in war_planner._rows("Units"):
                if r.get("UnitType") == self.unit_type:
                    return (r.get("Domain") or "") == "DOMAIN_SEA"
        except Exception:
            pass
        t = (self.unit_type or "").upper()
        return any(k in t for k in ("GALLEY", "QUADRIREME", "TRIREME", "FRIGATE"))

    @property
    def reach(self) -> int:
        """它下一回合能打到的最远距离(忽略地形的保守上界 —— 宁可高估敌人)。

        ⚠️ 海军只算射程,不算移动力(2026-08-07 苏美尔局实测)。
        船开不上陆地:一艘移动 3、射程 0 的蛮族桨帆船被算成"可达 3 格",
        于是内陆的建造者被 CIVILIAN_UNESCORTED 拦住 —— 而海军近战根本
        打不到陆地单位。误伤比漏拦贵:拦错几次就会养成无脑 force 的习惯。

        同一个修正 2026-08-06 已经在 `end_turn._threat_reach` 做过一次,
        但这条路径没同步 —— 又是"同一概念两套实现"。两处都改了,
        并各自有回归测试。
        """
        if self.is_naval:
            return max(1, self.range_)
        return max(1, self.moves) + self.range_

    @property
    def threatens(self) -> bool:
        """目标格是否在它下一回合的打击范围内。"""
        return self.dist <= self.reach


@dataclass
class MoveContext:
    """一次移动安全检查所需的全部事实,由单次 Lua 查询填充。"""

    found: bool = False
    error: str = ""
    unit_type: str = ""
    formation: str = ""
    promotion: str = ""
    moves_remaining: float = 0.0
    max_moves: float = 0.0
    from_x: int = 0
    from_y: int = 0
    # 目标格附近的可见敌方军事单位(含威胁推演)
    enemies: list[EnemySighting] = field(default_factory=list)
    # 目标格【同格】的己方军事单位数。只有同格才算真护送 —— 见查询里的说明。
    escorts: int = 0
    # 同格护卫里最高的近战战力,用于和威胁做强弱对比
    escort_cs: int = 0
    # 目标格【相邻】的己方军事单位数。不算护送,仅用于给出建议。
    adjacent_military: int = 0
    # 目标格是否是己方城市(城内平民安全)
    target_is_own_city: bool = False
    # —— 以下字段供 discipline.py 的 L4 执行纪律拦截使用(2026-08-03) ——
    # 该单位当前血量。同一次查询顺带带回,不新增 Lua 往返。
    hp: int = 0
    max_hp: int = 0
    # 目标格上是否有【敌方】城市,以及它的名字
    target_is_enemy_city: bool = False
    enemy_city_name: str = ""
    # 我方全部军事单位数(判「全国唯一军事单位」)
    my_military_total: int = 0
    # 目标格 REINFORCE_RADIUS 格内的己方军事单位数(不含发起单位与相邻的)
    nearby_military: int = 0
    # 自算的纯陆路,含起点,按顺序。空 = 没算出来(不适用/搜索被截断/无陆路)
    land_path: list[tuple[int, int]] = field(default_factory=list)
    # 陆地连通分量搜完仍未触到目标 —— 确定只能渡海。搜索被截断时【不】置位
    sea_only: bool = False
    # 目标格本身是水面。陆战单位到那儿就是登船状态,平民等于白送
    target_is_water: bool = False
    # 地形上有陆路,但被单位占位或封闭边境拦死 —— 这一步走不到
    route_blocked: bool = False

    @property
    def is_civilian(self) -> bool:
        return self.formation == CIVILIAN

    @property
    def hp_pct(self) -> float | None:
        """血量百分比。数据缺失返回 None —— 缺数据时不拦截,不猜。"""
        if self.max_hp <= 0:
            return None
        return 100.0 * self.hp / self.max_hp

    @property
    def is_ranged(self) -> bool:
        """远距攻击类(攻击不吃反击):远程/攻城/海军远程/空军。

        名字保留 is_ranged 是为了不动 4 个调用点;语义是 standoff。
        """
        return self.promotion in STANDOFF_CLASSES

    @property
    def has_moved(self) -> bool:
        """本回合是否已经移动过。"""
        return self.moves_remaining < self.max_moves


@dataclass
class Verdict:
    """拦截结论。blocked=False 表示放行。"""

    blocked: bool
    code: str = ""
    message: str = ""

    def as_error(self) -> str:
        return f"Error: {self.code}|{self.message}"


PASS = Verdict(blocked=False)


def build_move_safety_query(unit_id: int, tx: int, ty: int) -> str:
    """单次 Lua 查询,一并取回移动安全判定所需的全部事实。

    合并成一次往返是刻意的:实测单次查询约 324ms,拆成多次会把每回合
    拖到不可用。
    """
    return f"""
local me = Game.GetLocalPlayer()
local pDiplo = Players[me]:GetDiplomacy()
local pVis = PlayersVisibility[me]
local unit = Players[me]:GetUnits():FindID({unit_id})
if unit == nil then
    print("ERR|UNIT_NOT_FOUND")
    print("{SENTINEL}")
    return
end
if {unit_id} > 65535 and unit:GetID() ~= {unit_id} then
    print("ERR|UNIT_STALE")
    print("{SENTINEL}")
    return
end
local info = GameInfo.Units[unit:GetType()]
print("UNIT|" .. tostring(info and info.UnitType or "?")
    .. "|" .. tostring(info and info.FormationClass or "")
    .. "|" .. tostring(info and info.PromotionClass or "")
    .. "|" .. tostring(unit:GetMovesRemaining())
    .. "|" .. tostring(unit:GetMaxMoves())
    .. "|" .. tostring(unit:GetX()) .. "|" .. tostring(unit:GetY()))

print("HP|" .. tostring(unit:GetMaxDamage() - unit:GetDamage())
    .. "|" .. tostring(unit:GetMaxDamage()))

-- 目标格是否为己方城市(平民进城是安全的)
for _, c in Players[me]:GetCities():Members() do
    if c:GetX() == {tx} and c:GetY() == {ty} then print("INCITY|1") end
end

-- 目标格上是否有【敌方】城市(L4 攻城纪律拦截要用)。
-- 刻意不用 Cities.GetCityInPlot:该全局只在 InGame 上下文存在,本查询
-- 走 GameCore。遍历各家城市比坐标是两个上下文都成立的写法。
-- 整段 pcall:这一块失败绝不能连累同一次查询里的远程顺序检查
pcall(function()
    for pid2 = 0, 63 do
        if pid2 ~= me and Players[pid2] and Players[pid2]:IsAlive() then
            for _, c in Players[pid2]:GetCities():Members() do
                if c:GetX() == {tx} and c:GetY() == {ty} then
                    local cname = c:GetName() or ""
                    pcall(function() cname = Locale.Lookup(c:GetName()) end)
                    print("ENEMYCITY|" .. tostring(cname))
                end
            end
        end
    end
end)

-- 目标格附近的可见敌方军事单位。可见性规则与 build_threat_scan_query 一致。
for pid = 0, 63 do
    if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
        local isBarb = (pid == 63)
        -- 2026-08-02 校准局 4d(与 4c 同根):此前只过滤城邦,中立主要文明
        -- 的侦察兵也会触发 CIVILIAN_UNESCORTED 拦截平民移动。和平状态的
        -- 单位不会俘虏平民 —— 只有蛮族与已宣战方算威胁。
        if not isBarb and not pDiplo:IsAtWarWith(pid) then
            -- 未交战(文明或城邦)不算威胁
        else
            local ownerName = "Barbarian"
            if pid ~= 63 then
                local cfg = PlayerConfigurations[pid]
                if cfg then ownerName = Locale.Lookup(cfg:GetCivilizationShortDescription()) end
            end
            for _, eu in Players[pid]:GetUnits():Members() do
                local ex, ey = eu:GetX(), eu:GetY()
                if ex ~= -9999 and pVis:IsVisible(ex, ey) then
                    local e = GameInfo.Units[eu:GetType()]
                    local cs = (e and e.Combat) or 0
                    local rcs = math.max(
                        tonumber(e and e.RangedCombat) or 0,
                        tonumber(e and e.Bombard) or 0)
                    if cs > 0 or rcs > 0 then
                        local d = Map.GetPlotDistance(ex, ey, {tx}, {ty})
                        if d <= {SCAN_RADIUS} then
                            print("ENEMY|" .. ex .. "|" .. ey .. "|" .. d .. "|"
                                .. tostring(e and e.UnitType or "?") .. "|"
                                .. math.max(cs, rcs) .. "|" .. ownerName .. "|"
                                .. tostring((e and e.BaseMoves) or 2) .. "|"
                                .. tostring((e and e.Range) or 0))
                        end
                    end
                end
            end
        end
    end
end

-- 护送判定。文明6里平民要和军事单位【同格】才受保护 —— 敌方单位必须先
-- 消灭同格的护卫才能碰到平民。相邻的军事单位挡不住敌人绕过去直接抓平民,
-- 所以只有 distance == 0 才算真护送;相邻的单独统计,只用于提示。
local escorts = 0
local escortCS = 0
local adjacent = 0
local milTotal = 0
local nearby = 0
for _, u in Players[me]:GetUnits():Members() do
    local ui = GameInfo.Units[u:GetType()]
    local uf = ui and ui.FormationClass or ""
    local ucs = (ui and ui.Combat) or 0
    -- 攻城类的远程在 Bombard 列(T291):漏掉它,我方投石机/大炮不算军事单位,
    -- "全国唯一军事单位"这类判据会数错。
    local urs = math.max(
        tonumber(ui and ui.RangedCombat) or 0,
        tonumber(ui and ui.Bombard) or 0)
    local isMil = (uf ~= "{CIVILIAN}") and (ucs > 0 or urs > 0)
    if isMil then milTotal = milTotal + 1 end
    if u:GetID() ~= {unit_id} then
        if uf ~= "{CIVILIAN}" and ucs > 0 then
            local ux, uy = u:GetX(), u:GetY()
            if ux ~= -9999 then
                local d = Map.GetPlotDistance(ux, uy, {tx}, {ty})
                if d == 0 then
                    escorts = escorts + 1
                    if ucs > escortCS then escortCS = ucs end
                elseif d == 1 then
                    adjacent = adjacent + 1
                elseif d <= {REINFORCE_RADIUS} then
                    -- 还没到发起线、但两回合内能到 —— 这些就是"值得等的援军"
                    nearby = nearby + 1
                end
            end
        end
    end
end
print("ESCORTS|" .. escorts)
print("ESCORTCS|" .. escortCS)
print("ADJACENT|" .. adjacent)
print("MYMIL|" .. milTotal)
print("NEARBY|" .. nearby)

-- 陆路求解(2026-08-11 罗马局 T267 实锤,复盘里第 7 次)。
--
-- 病象:目标格是【陆地】,引擎寻路却从水面抄近道,陆战单位自动登船。
-- 登船 = 战力归零、一击即死。T267 一个线列步兵就这样漂在英国海域上。
--
-- 走过一段弯路,记在这里免得后人重走:第一版想问引擎"你打算走哪条路"
-- (UnitManager.GetMoveToPath)。该函数在 GameCore 里【存在且可调用】,
-- 但签名是 (unit, plotIndex) 而不是 (unit, x, y) —— 传 x,y 时第二个参数
-- 被当成 plotIndex,于是 (62,48)→(63,49) 这种相邻格移动返回了一条 51 格、
-- 含 33 个水格、绕经地图最北端的路径。数字看着像真的,所以差点就信了。
-- 教训:同一个荒谬数字在不同目标下【一模一样】,那就不是测量值。
--
-- 所以不问引擎,自己算。这里做的是一次有界 BFS,只走陆地且可通行的格,
-- 带父指针,把整条陆路还原出来交给调用方【逐格下单】—— 每一步都是相邻格,
-- 引擎就没有抄近道的余地。这比拦截更进一步:不是拦住调用方,是替它走对。
--
--   LANDSTEP|i|x|y —— 陆路第 i 格(含起点),按顺序
--   LANDPATH|NONE  —— 陆地连通分量搜完仍未触到目标 → 只能渡海
--   (无)           —— 搜索触到上限,结论不确定 → 放行,不拦也不改写
pcall(function()
    local uInfo = GameInfo.Units[unit:GetType()]
    if (uInfo and uInfo.Domain or "") ~= "DOMAIN_LAND" then return end
    local tPlot = Map.GetPlot({tx}, {ty})
    if tPlot == nil then return end
    -- 目标格【本身就是水】—— 陆战单位到那儿必然是登船状态。
    -- 2026-08-11 T270 实机:我让建造者去 (69,55) 改良石油,没注意那是【海上】
    -- 油田,建造者一头扎进水里、战力归零漂在那儿。旧代码在这里直接 return,
    -- 于是这条路径上一个结论都不给,门等于不存在。
    if tPlot:IsWater() then print("LANDPATH|WET") return end
    local sx, sy = unit:GetX(), unit:GetY()
    if sx == {tx} and sy == {ty} then return end
    -- 己方同编队类单位占着的格要绕开:文明6 不允许同类堆叠,把路径规划
    -- 到这种格上,第一步就会撞 STACKING_CONFLICT。前线越密集越常见 ——
    -- T267 首次实测就撞上了(62,49 站着自己的野战加农炮)。
    -- 先把位置收成查找表,别在 BFS 里逐格查 Map.GetUnitsAt。
    local myClass = (uInfo and uInfo.FormationClass) or ""
    local blocked = {{}}
    for _, ou in Players[me]:GetUnits():Members() do
        if ou:GetID() ~= unit:GetID() then
            local oi = GameInfo.Units[ou:GetType()]
            if oi and oi.FormationClass == myClass then
                local ox, oy = ou:GetX(), ou:GetY()
                if ox ~= -9999 then blocked[ox * 1000 + oy] = true end
            end
        end
    end
    -- 外国单位占的格同样走不进去:move 到敌占格会被拒(非交战方更是直接
    -- NOT_AT_WAR),所以路径算到那儿就是一条走不通的路。
    -- 2026-08-11 T277 实机:大科学家和大作家同一回合分别被普雷斯拉夫的
    -- 装甲剑士和维尔纽斯的长矛兵挡住 —— 城邦单位在我自己的领土里游荡,
    -- 这不是偶发,是常态。只算可见单位:看不见的挡路挡不到规划头上。
    pcall(function()
        local pv = PlayersVisibility[me]
        for pid = 0, 63 do
            if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
                for _, fu in Players[pid]:GetUnits():Members() do
                    local fx, fy = fu:GetX(), fu:GetY()
                    if fx ~= -9999 and pv:IsVisible(fx, fy) then
                        blocked[fx * 1000 + fy] = true
                    end
                end
            end
        end
    end)
    -- 没有开放边境的外国【领土】也进不去(T278 实机:大科学家去自然奇观,
    -- 被俄罗斯领土挡住报 need Open Borders)。先把可通行的归属算成一张表,
    -- BFS 里查表,别在每格重算外交状态。
    local passable = {{}}
    passable[me] = true
    passable[-1] = true  -- 无主地
    pcall(function()
        local dip = Players[me]:GetDiplomacy()
        for pid = 0, 63 do
            if pid ~= me and Players[pid] and Players[pid]:IsAlive() then
                local ok = false
                pcall(function() ok = dip:HasOpenBordersFrom(pid) end)
                -- 交战中反而能进 —— 战争本身就是通行证
                pcall(function() ok = ok or dip:IsAtWarWith(pid) end)
                -- 城邦不走开放边境那套:只认宗主。非宗主的领土(陆地和领海
                -- 都算)一律封闭 —— 2026-08-11 T278 实机,大科学家去自然奇观
                -- 被桑给巴尔挡住,报的是 need suzerainty 而不是 Open Borders。
                pcall(function()
                    if not Players[pid]:IsMajor() then
                        local suz = -1
                        pcall(function() suz = Players[pid]:GetInfluence():GetSuzerain() end)
                        ok = (suz == me)
                    end
                end)
                passable[pid] = ok
            end
        end
    end)
    -- 跑两遍,因为两个问题的答案不同:
    --   带单位阻挡 —— 这一步【实际】能不能走通,用来铺路径
    --   只看地形   —— 到底【有没有】陆路,用来判 sea_only
    -- 混成一遍就会把"被城邦单位堵了一格"误报成"只能渡海",然后拦下一次
    -- 完全正常的调动。单位每回合都在动,地形不动 —— 结论不该由前者决定。
    local function bfs(useUnits)
        local seen, prev, queue, head = {{}}, {{}}, {{{{sx, sy}}}}, 1
        seen[sx * 1000 + sy] = true
        local found, truncated, expanded = false, false, 0
        while head <= #queue and not found do
            local cur = queue[head]
            head = head + 1
            if expanded >= {LAND_PROBE_LIMIT} then truncated = true break end
            expanded = expanded + 1
            for dir = 0, 5 do
                local adj = Map.GetAdjacentPlot(cur[1], cur[2], dir)
                if adj ~= nil then
                    local ax, ay = adj:GetX(), adj:GetY()
                    local k = ax * 1000 + ay
                    if not seen[k] then
                        seen[k] = true
                        prev[k] = cur[1] * 1000 + cur[2]
                        if ax == {tx} and ay == {ty} then found = true break end
                        local owner = adj:GetOwner()
                        local mayEnter = (passable[owner] == true)
                        if (not adj:IsWater()) and (not adj:IsImpassable())
                                and not (useUnits and (blocked[k] or not mayEnter)) then
                            queue[#queue + 1] = {{ax, ay}}
                        end
                    end
                end
            end
        end
        return found, truncated, prev
    end

    local found, truncated, prev = bfs(true)
    if found then
        local chain, k = {{}}, {tx} * 1000 + {ty}
        while k ~= nil do
            table.insert(chain, 1, k)
            k = prev[k]
        end
        for i, kk in ipairs(chain) do
            print("LANDSTEP|" .. i .. "|" .. math.floor(kk / 1000) .. "|" .. (kk % 1000))
        end
    elseif not truncated then
        -- 带单位阻挡走不通。是地形不通,还是只是被谁堵了一格?
        local dryFound, dryTrunc = bfs(false)
        if not dryFound and not dryTrunc then
            print("LANDPATH|NONE")
        elseif dryFound then
            -- 地形通得过,却被单位或封闭边境拦住。不拦截 —— 挡路的单位
            -- 下回合就走开了,拦一次调动的代价比放行大。但必须【说出来】:
            -- 2026-08-11 T278 我对着同一个目标连发三次,每次引擎都一头
            -- 撞进桑给巴尔边境,而工具早就算出这条路走不通,只是没讲。
            -- 静默放行等于让调用方去撞墙,而撞墙的代价是一整回合移动力。
            print("LANDPATH|BLOCKED")
        end
    end
end)
print("{SENTINEL}")
"""


def parse_move_safety(lines: list[str]) -> MoveContext:
    ctx = MoveContext()
    steps: list[tuple[int, int, int]] = []
    for line in lines:
        if line.startswith("ERR|"):
            ctx.error = line.split("|", 1)[1]
            return ctx
        if line.startswith("UNIT|"):
            p = line.split("|")
            ctx.found = True
            ctx.unit_type = p[1]
            ctx.formation = p[2]
            ctx.promotion = p[3]
            try:
                ctx.moves_remaining = float(p[4])
                ctx.max_moves = float(p[5])
                ctx.from_x, ctx.from_y = int(p[6]), int(p[7])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ENEMY|"):
            p = line.split("|")
            try:
                ctx.enemies.append(
                    EnemySighting(
                        x=int(p[1]),
                        y=int(p[2]),
                        dist=int(p[3]),
                        unit_type=p[4],
                        cs=int(p[5]),
                        owner=p[6],
                        # 旧格式(6 字段)没有机动数据 → 保守兜底
                        moves=int(p[7]) if len(p) > 7 else FALLBACK_ENEMY_MOVES,
                        range_=int(p[8]) if len(p) > 8 else 0,
                    )
                )
            except (ValueError, IndexError):
                continue
        elif line.startswith("ESCORTS|"):
            try:
                ctx.escorts = int(line.split("|")[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ESCORTCS|"):
            try:
                ctx.escort_cs = int(line.split("|")[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ADJACENT|"):
            try:
                ctx.adjacent_military = int(line.split("|")[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("INCITY|"):
            ctx.target_is_own_city = True
        elif line.startswith("HP|"):
            p = line.split("|")
            try:
                ctx.hp, ctx.max_hp = int(p[1]), int(p[2])
            except (ValueError, IndexError):
                pass
        elif line.startswith("ENEMYCITY|"):
            ctx.target_is_enemy_city = True
            ctx.enemy_city_name = line.split("|", 1)[1].strip()
        elif line.startswith("MYMIL|"):
            try:
                ctx.my_military_total = int(line.split("|")[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("NEARBY|"):
            try:
                ctx.nearby_military = int(line.split("|")[1])
            except (ValueError, IndexError):
                pass
        elif line.startswith("LANDSTEP|"):
            p = line.split("|")
            try:
                steps.append((int(p[1]), int(p[2]), int(p[3])))
            except (ValueError, IndexError):
                continue
        elif line.startswith("LANDPATH|NONE"):
            ctx.sea_only = True
        elif line.startswith("LANDPATH|WET"):
            ctx.target_is_water = True
        elif line.startswith("LANDPATH|BLOCKED"):
            ctx.route_blocked = True
    # 按序号排序后落库 —— 不依赖 Lua 的输出顺序
    ctx.land_path = [(x, y) for _, x, y in sorted(steps)]
    return ctx


def _describe(enemies: list[EnemySighting]) -> str:
    """把敌军列表压成一行人类可读的描述,含"它下回合能到"的推演。"""
    parts = []
    for e in sorted(enemies, key=lambda e: e.dist)[:4]:
        name = e.unit_type.replace("UNIT_", "")
        parts.append(
            f"{e.owner} {name}(CS{e.cs} 机动{e.moves}"
            + (f"+射程{e.range_}" if e.range_ else "")
            + f") 在({e.x},{e.y}) 距{e.dist}格,下回合可达 {e.reach} 格内"
        )
    return " ‖ ".join(parts)


def check_move(ctx: MoveContext, tx: int, ty: int, force: bool = False) -> Verdict:
    """移动前的安全判定。

    规则 1(平民保护)+ 规则 2(开拓者护送)合并:
    平民单位移动到【任一可见敌军下回合打击范围内】的格子,且目标不是己方城市、
    也没有同格护卫时,拒绝执行。

    危险判定是【对手推演】,不是固定半径:每个敌军按自己的 移动力+射程 算出
    下回合可达范围,目标格落进任何一个就算危险。

    这条改动来自用户 2026-07-31 的想法:"不知道对面会干什么,就假设自己是对面"。
    对蛮族这种确定性规则机,推演是计算而非猜测 —— 只算能力上界,不猜意图。
    初版用固定 2 格,而复盘 T33 抓死开拓者的是移动力 4 的骑手:固定半径放行,
    动态推演拦下。

    这一条对应复盘里 5 个死亡:建造者×3、开拓者×2。
    """
    if force or not ctx.found:
        return PASS

    if not ctx.is_civilian:
        return PASS

    if ctx.target_is_own_city:
        return PASS  # 进自己城里是安全的

    # 核心:目标格是否落在某个敌军下一回合的打击范围内
    near = [e for e in ctx.enemies if e.threatens]
    if not near:
        return PASS

    name = ctx.unit_type.replace("UNIT_", "")
    threat_cs = max(e.cs for e in near)

    if ctx.escorts > 0:
        # 有同格护卫。但护卫战力远低于威胁时形同虚设 —— 复盘 T27 就是
        # 投石兵(CS5)"看着"建造者,面对 CS20 的骑手,建造者照样被杀。
        if ctx.escort_cs * 2 >= threat_cs:
            return PASS
        return Verdict(
            blocked=True,
            code="ESCORT_TOO_WEAK",
            message=(
                f"{name} 在 ({tx},{ty}) 虽有同格护卫,但护卫战力 CS{ctx.escort_cs}"
                f" 远低于威胁 CS{threat_cs},挡不住。附近敌军:{_describe(near)}。"
                f" 可选做法:①换战力更高的单位护送;②先撤回城内;"
                f" ③先用远程消耗威胁再动平民。"
                f" 确认要冒险请重调并传 force=True。"
            ),
        )

    hint = ""
    if ctx.adjacent_military > 0:
        # 这是最容易踩的误解,值得单独点破
        hint = (
            f" 注意:目标格【相邻】有 {ctx.adjacent_military} 个己方军事单位,"
            f"但相邻不算护送 —— 文明6里平民必须和军事单位【同格】才受保护,"
            f"否则敌人会绕过护卫直接抓平民。"
        )

    return Verdict(
        blocked=True,
        code="CIVILIAN_UNESCORTED",
        message=(
            f"{name} 是平民单位(防御力 0),移动到 ({tx},{ty}) 会被杀死或俘虏。"
            f" 按敌军自身机动力推演,以下单位【下回合能打到该格】:"
            f"{_describe(near)}。"
            f" 目标格上没有己方军事单位同格护送。{hint}"
            f" 可选做法:①先把军事单位移到 ({tx},{ty}),再让平民移到同一格;"
            f" ②改去己方城市内暂避;③先清除威胁。"
            f" 确认要冒险请重调并传 force=True。"
        ),
    )


def check_embark(ctx: MoveContext, tx: int, ty: int, force: bool = False) -> Verdict:
    """跨海拦截(规则 5)。目标是陆地,但陆路根本不存在 —— 拦在下指令之前。

    2026-08-11 罗马局 T267:一条两格调动,引擎从海湾抄近道,线列步兵登船
    漂在英国海域上,战力归零。此前工具只有【事后】警告 —— 而事后救不了:
    移动力已经耗尽,当回合撤不回来。

    "抄近道"那一类不在这里拦,而是由 land_path 直接绕开(见 plan_land_hops):
    能替调用方走对的事,不该变成一次拒绝。这里只管【确实没有陆路】的情况:
    那是一次真正的跨海投送,登船的代价必须是明知故犯,不能是顺手一点。
    """
    if force or not ctx.found:
        return PASS

    name = ctx.unit_type.replace("UNIT_", "")

    if ctx.target_is_water:
        # 海上资源最容易骗到人:任务板把 OIL@69,55 和陆地资源并排列出来,
        # 看不出那一格是海。建造者走过去 = 登船 = 战力归零的平民漂在海上,
        # 而且它到了也改良不了 —— 海上资源要海军或特定单位。
        extra = "而且平民到了水上既改良不了也自保不了" if ctx.is_civilian else ""
        return Verdict(
            blocked=True,
            code="TARGET_IS_WATER",
            message=(
                f"({tx},{ty}) 是【水面格】,{name} 是陆战单位,过去就是登船状态:"
                f"战力归零,任何单位一击就能杀死它。{extra}"
                f" 常见来源:海上资源(石油/鲸鱼/螃蟹)在任务板上和陆地资源"
                f"长得一样。 可选做法:①换一个陆地目标;"
                f"②海上资源要用海军单位或等对应科技。"
                f" 确认要下水请重调并传 force=True。"
            ),
        )

    if ctx.route_blocked:
        # 陆路存在但被单位/封闭边境拦死。这一条最初只报不拦,理由是
        # "挡路的下回合就走开了,拦一次调动的代价比放行大"。
        # 2026-08-11 T287 实测把这个权衡推翻了:放行之后引擎并不会原地不动,
        # 它会绕远 —— 从水面绕。一个建造者就这样从 (69,57) 漂到了 (69,59),
        # 战力归零。拦一回合是可逆的,单位下水不是;而且此时【没有陆路 hops
        # 可以接管】,放行等于把方向盘交还给刚被证明会开进海里的那个寻路器。
        return Verdict(
            blocked=True,
            code="ROUTE_BLOCKED",
            message=(
                f"{name} 去 ({tx},{ty}) 的陆路被单位占位或封闭边境拦死。"
                f" 直接放行的话引擎会绕水路走,单位登船、战力归零"
                f"(T287 一个建造者就这样漂进海里)。"
                f" 可选做法:①等挡路的单位走开(通常一两回合);"
                f"②先谈开放边境或取得城邦宗主;"
                f"③改一个陆路通得到的目标;④分段下单,一格一格挪。"
                f" 确认要让引擎自己走(可能下水)请重调并传 force=True。"
            ),
        )

    if not ctx.sea_only:
        return PASS

    return Verdict(
        blocked=True,
        code="EMBARK_REQUIRED",
        message=(
            f"{name} 去 ({tx},{ty}) 没有陆路可走,只能渡海。"
            f" 登船期间战力归零,任何单位一击就能杀死它,"
            f"中途停在海上更是白送。"
            f" 可选做法:①先造/调海军护航,和运输一起动;"
            f"②确认全程能在一回合内上岸再走;"
            f"③改走陆路可达的目标。"
            f" 确认要渡海请重调并传 force=True。"
        ),
    )


# 单次移动最多替调用方走几格。多回合的长途仍然一回合走一段 —— 这个上限
# 只是防止一条几十格的路径在一次调用里打出几十次 Lua 写入。
MAX_LAND_HOPS = 8


def plan_land_hops(ctx: MoveContext, tx: int, ty: int) -> list[tuple[int, int]]:
    """把一次移动拆成沿陆路的相邻格序列。空列表 = 照原样交给引擎。

    这是 T267 的真修复。引擎对 (62,48)→(63,51) 这条三格调动规划了一条
    51 格、含 33 个水格、绕经地图最北端的路线;同一段路的纯陆路是
    (62,48)→(62,49)→(63,50)→(63,51),BFS 七次展开就找到了。

    逐格下单之后引擎没有抄近道的余地:每一步都是相邻格,不存在"绕远"
    这个选项。所以这里不拦截、不要求 force —— 直接走对。
    """
    path = ctx.land_path
    # 少于三格说明目标就在隔壁,引擎绕不出花样,不必接管
    if len(path) < 3 or path[-1] != (tx, ty):
        return []
    return path[1 : 1 + MAX_LAND_HOPS]


def check_ranged_attack(ctx: MoveContext, force: bool = False) -> Verdict:
    """远程单位攻击顺序检查(规则 3)。

    远程攻击会消耗**剩余**移动力,所以只有移动力已经归零时才打不出去。

    2026-08-11 罗马局 T229:此前判据是 has_moved —— 只要动过一格就拦。
    可攻城的标准动作恰恰是「推进到射程内再开火」:野战加农炮 2 移动,
    走 1 格到 (80,52) 后仍剩 1.0,对沃罗涅什开火**完全合法**,实测打掉
    18 点城墙(100→82)。门禁把它拦了,理由写的还是「现在攻击会失败」。
    这条误报比它防的错更贵:它挡住的是整个攻城流程,而且每次都要 force。
    真正该拦的只有一种情况 —— 移动力已经耗尽还想攻击。
    """
    if force or not ctx.found:
        return PASS
    if not ctx.is_ranged:
        return PASS
    try:
        if float(ctx.moves_remaining) > 0:
            return PASS
    except (TypeError, ValueError):
        # 读不到剩余移动力时退回旧判据,宁可多问一句
        if not ctx.has_moved:
            return PASS

    name = ctx.unit_type.replace("UNIT_", "")
    return Verdict(
        blocked=True,
        code="RANGED_ALREADY_MOVED",
        message=(
            f"{name} 是远程单位,本回合移动力已经耗尽"
            f"(剩余 {ctx.moves_remaining}/{ctx.max_moves}),现在攻击会失败。"
            f" 远程单位只要还剩移动力就能开火(攻击会吃掉剩下的移动力),"
            f" 所以「推进到射程内再开火」是可以的 —— 但走到一格不剩就打不了了。"
            f" 本回合改用其他单位,或下回合原地先攻击再考虑移动。"
            f" 确认要试请传 force=True。"
        ),
    )
