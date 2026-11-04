-- GARNT TOOL REJOIN heartbeat (auto-generated)
-- ใช้กับบัญชีและเครื่องของตัวเองเท่านั้น ต้องรันผ่านเอ็กซิคิวเตอร์ที่รองรับ HTTP
local HOST     = "http://127.0.0.1:8765"
local ID       = "auto"       -- ชื่อแพ็กเกจหรือเลขอินสแตนซ์ ต้องตรงกับในเครื่องมือ
local INTERVAL = 15   -- ส่งทุกกี่วินาที
local SHOW_FPS = true    -- true = แสดงตัวเลข FPS บนจอ (ลากย้ายได้)
local DEFAULT_CAP = 0  -- FPS cap ของบัญชีที่ไม่อยู่ในรายการ (0 = ไม่แตะ)
local CAPS = {                  -- FPS cap ต่อบัญชี: [Roblox UserId] = FPS (ตั้งจากเมนูในเครื่องมือ)

}

if getgenv then
    if getgenv().__GARNT_HB then return end
    getgenv().__GARNT_HB = true
end

if not game:IsLoaded() then game.Loaded:Wait() end
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local GuiService = game:GetService("GuiService")
local UserInputService = game:GetService("UserInputService")

local lp = Players.LocalPlayer
while not lp do task.wait(1); lp = Players.LocalPlayer end

-- FPS cap: ดู UserId ของบัญชีที่กำลังเล่นอยู่ แล้วตั้งเพดานตามที่ตั้งไว้ (ต้องมี setfpscap ในเอ็กซิคิวเตอร์)
local function applyCap()
    local cap = CAPS[lp.UserId] or DEFAULT_CAP
    if type(cap) ~= "number" or cap <= 0 then return end
    local fn = setfpscap or set_fps_cap or (getgenv and getgenv().setfpscap)
    if fn then pcall(fn, cap) end
end
pcall(applyCap)

local lastFrame = os.clock()
local frames, fps, winStart = 0, 0, os.clock()
RunService.Heartbeat:Connect(function()
    local now = os.clock()
    lastFrame = now
    frames = frames + 1
    if now - winStart >= 1 then
        fps = math.floor(frames / (now - winStart) + 0.5)
        frames, winStart = 0, now
    end
end)

local function httpGet(url)
    local req = (syn and syn.request) or http_request or request or (http and http.request)
    if req then
        local ok = pcall(req, { Url = url, Method = "GET" })
        if ok then return true end
    end
    return (pcall(function() return game:HttpGet(url) end))
end

local function errorText()
    local ok, msg = pcall(function() return GuiService:GetErrorMessage() end)
    if ok and type(msg) == "string" and msg ~= "" then return msg end
    return nil
end

local function beat()
    if os.clock() - lastFrame > 5 then return end   -- เกมค้าง: ไม่ส่ง
    local url = string.format("%s/hb?id=%s&user=%s&uid=%d&place=%d&job=%s&fps=%d", HOST,
        HttpService:UrlEncode(ID), HttpService:UrlEncode(lp.Name), lp.UserId, game.PlaceId,
        HttpService:UrlEncode(game.JobId), fps)
    local err = errorText()                          -- ขึ้นหน้าต่าง Disconnected: ส่งข้อความ error ทันที
    if err then url = url .. "&err=" .. HttpService:UrlEncode(string.sub(err, 1, 120)) end
    httpGet(url)
end

pcall(function() GuiService.ErrorMessageChanged:Connect(function() pcall(beat) end) end)

task.spawn(function()
    while true do
        pcall(applyCap)                              -- ตั้งซ้ำทุกรอบ เผื่อเกม/เอ็กซิคิวเตอร์รีเซ็ตค่า
        pcall(beat)
        task.wait(INTERVAL)
    end
end)

-- ตัวเลข FPS: ไม่มีกรอบ ฟอนต์กลม ขนาดปรับตามความกว้างจออัตโนมัติ ลากไปวางที่ไหนก็ได้
-- (จำตำแหน่งไว้ถ้าเอ็กซิคิวเตอร์รองรับ writefile)
local POS_FILE = "garnt_fps_pos.txt"

local function parentGui(gui)
    local ok = false
    if gethui then ok = pcall(function() gui.Parent = gethui() end) end
    if not ok or not gui.Parent then
        ok = pcall(function() gui.Parent = game:GetService("CoreGui") end)
    end
    if not ok or not gui.Parent then
        local pg = lp:FindFirstChildOfClass("PlayerGui") or lp:WaitForChild("PlayerGui", 10)
        if pg then gui.Parent = pg end
    end
    return gui.Parent ~= nil
end

local function loadPos()
    if not (isfile and readfile) then return nil end
    local ok, data = pcall(function() if isfile(POS_FILE) then return readfile(POS_FILE) end end)
    if ok and type(data) == "string" then
        local x, y = string.match(data, "^(%-?%d+),(%-?%d+)$")
        if x then return tonumber(x), tonumber(y) end
    end
    return nil
end

local function savePos(x, y)
    if writefile then pcall(writefile, POS_FILE, string.format("%d,%d", x, y)) end
end

local function roundFont()
    for _, name in ipairs({ "FredokaOne", "Nunito", "GothamBold" }) do   -- ตัวกลมก่อน ถ้าไม่มีค่อยถอยไปตัวถัดไป
        local ok, f = pcall(function() return Enum.Font[name] end)
        if ok and f then return f end
    end
    return Enum.Font.SourceSansBold
end

local function buildFpsBox()
    local gui = Instance.new("ScreenGui")
    gui.Name = "GARNT_FPS"
    gui.ResetOnSpawn = false
    gui.IgnoreGuiInset = true
    gui.DisplayOrder = 999999
    if not parentGui(gui) then return end

    local box = Instance.new("Frame")
    box.Name = "Box"
    box.BackgroundColor3 = Color3.fromRGB(0, 0, 0)       -- พื้นดำ ไม่มีกรอบ
    box.BackgroundTransparency = 0.25
    box.BorderSizePixel = 0
    box.Active = true
    box.Parent = gui

    local corner = Instance.new("UICorner")
    corner.CornerRadius = UDim.new(1, 0)                 -- ขอบมนเต็มที่
    corner.Parent = box

    local label = Instance.new("TextLabel")
    label.BackgroundTransparency = 1
    label.Size = UDim2.new(1, 0, 1, 0)
    label.Font = roundFont()
    label.TextScaled = true
    label.TextColor3 = Color3.fromRGB(255, 255, 255)
    label.Text = "FPS --"
    label.Parent = box

    local pad = Instance.new("UIPadding")
    pad.PaddingLeft = UDim.new(0.12, 0)
    pad.PaddingRight = UDim.new(0.12, 0)
    pad.PaddingTop = UDim.new(0.14, 0)
    pad.PaddingBottom = UDim.new(0.14, 0)
    pad.Parent = label

    local W, H = 72, 28
    local function viewport()
        local cam = workspace.CurrentCamera
        return (cam and cam.ViewportSize) or Vector2.new(800, 600)
    end

    local function clamp(x, y)
        local vp = viewport()
        return math.clamp(x, 0, math.max(0, vp.X - W)), math.clamp(y, 0, math.max(0, vp.Y - H))
    end

    local function layout()                              -- ขนาดตามความกว้างจอ: เล็กกระทัดรัดแต่ยังอ่านออก
        local vp = viewport()
        W = math.clamp(math.floor(vp.X * 0.055), 56, 110)
        H = math.floor(W * 0.4)
        box.Size = UDim2.fromOffset(W, H)
        local x, y = clamp(box.Position.X.Offset, box.Position.Y.Offset)
        box.Position = UDim2.fromOffset(x, y)
    end

    local sx, sy = loadPos()
    if not sx then sx, sy = 12, 64 end
    box.Position = UDim2.fromOffset(sx, sy)
    layout()

    local cam = workspace.CurrentCamera
    if cam then
        cam:GetPropertyChangedSignal("ViewportSize"):Connect(function() pcall(layout) end)   -- หมุนจอ/ย่อขยายหน้าต่าง
    end

    local dragging, dragStart, startX, startY = false, nil, 0, 0
    box.InputBegan:Connect(function(input)
        local t = input.UserInputType
        if t == Enum.UserInputType.MouseButton1 or t == Enum.UserInputType.Touch then
            dragging = true
            dragStart = input.Position
            startX, startY = box.Position.X.Offset, box.Position.Y.Offset
            input.Changed:Connect(function()
                if input.UserInputState == Enum.UserInputState.End then
                    dragging = false
                    savePos(box.Position.X.Offset, box.Position.Y.Offset)
                end
            end)
        end
    end)
    UserInputService.InputChanged:Connect(function(input)
        if not dragging then return end
        local t = input.UserInputType
        if t == Enum.UserInputType.MouseMovement or t == Enum.UserInputType.Touch then
            local x, y = clamp(startX + (input.Position.X - dragStart.X), startY + (input.Position.Y - dragStart.Y))
            box.Position = UDim2.fromOffset(x, y)
        end
    end)

    while gui.Parent do
        label.Text = (fps > 0) and ("FPS " .. tostring(fps)) or "FPS --"
        task.wait(0.5)
    end
end

if SHOW_FPS then
    task.spawn(function() pcall(buildFpsBox) end)
end
