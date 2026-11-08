-- Reaper to Pro Tools — export plug-in library
-- Run once in Reaper: Actions > Show action list > New action > Load ReaScript... > choose this file > Run.
-- The script adds each wanted plug-in to a scratch track, copies the FX block REAPER itself writes (with the plug-in's
-- default state) and stores it in ~/Library/Application Support/Reaper to Pro Tools/fx_library.txt.
-- Plug-ins to export: names listed in wanted.txt (same folder); if there is none, every installed VST3/VST/AU/CLAP plug-in.
-- If a plug-in crashes REAPER, just run the script again: it remembers progress and skips the plug-in that crashed.

local HOME = os.getenv("HOME")
local DIR = HOME .. "/Library/Application Support/Reaper to Pro Tools/"
os.execute('mkdir -p "' .. DIR .. '"')
local LIB, STATE, WANTED = DIR .. "fx_library.txt", DIR .. "fx_export_state.txt", DIR .. "wanted.txt"

local function norm(s) return (s:lower():gsub("[^%w]", "")) end

local function read_lines(path)
  local t, f = {}, io.open(path, "r")
  if f then for l in f:lines() do t[#t + 1] = l end f:close() end
  return t
end

-- progress: name -> "done" | "crash" | "fail"
local status, last = {}, nil
for _, l in ipairs(read_lines(STATE)) do
  local k, name = l:match("^(%a+)|(.*)$")
  if k then status[name] = k; last = (k == "current") and name or nil end
end
if last then                                    -- previous run died while loading this plug-in
  status[last] = "crash"
  local f = io.open(STATE, "a"); f:write("crash|" .. last .. "\n"); f:close()
end

local wanted = {}
for _, l in ipairs(read_lines(WANTED)) do
  local n = norm(l)
  if n ~= "" then wanted[#wanted + 1] = n end
end

local function is_wanted(name)
  if #wanted == 0 then return true end
  local b = name:gsub("^[%w]+i?:%s*", "")                    -- without the format prefix
  while true do                                              -- strip trailing "(vendor)", "(x64)", "(12ch)" ...
    local nb = b:gsub("%s*%b()%s*$", "")
    if nb == b then break end
    b = nb
  end
  local base = norm(b)
  for _, w in ipairs(wanted) do
    if base:find(w, 1, true) or (#base >= 4 and w:find(base, 1, true)) then return true end
  end
  return false
end

-- candidate plug-ins
local todo = {}
local i = 0
while true do
  local ok, name = reaper.EnumInstalledFX(i)
  if not ok then break end
  i = i + 1
  local kind = name:match("^(%w+):")
  if kind and (kind == "VST3" or kind == "VST3i" or kind == "VST" or kind == "VSTi" or kind == "AU" or kind == "AUi" or kind == "CLAP" or kind == "CLAPi")
     and not name:find("%(Cockos%)") and is_wanted(name) and not status[name] then
    todo[#todo + 1] = name
  end
end

reaper.ClearConsole()
reaper.ShowConsoleMsg(string.format("Reaper to Pro Tools: %d plug-ins to export\n", #todo))
if #todo == 0 then reaper.ShowConsoleMsg("Nothing to do.\n") return end

reaper.Undo_BeginBlock()
reaper.InsertTrackAtIndex(reaper.CountTracks(0), false)
local track = reaper.GetTrack(0, reaper.CountTracks(0) - 1)

local function block_of(chunk)
  local lines, inchain, depth, out = {}, false, 0, nil
  for l in (chunk .. "\n"):gmatch("(.-)\n") do lines[#lines + 1] = l end
  local n = 1
  while n <= #lines do
    local t = lines[n]:match("^%s*(.-)%s*$")
    if not inchain then
      if t:match("^<FXCHAIN") then inchain = true end
    elseif not out and t:match("^<[%u]+%s") and not t:match("^<FXCHAIN") then
      out, depth = {t}, 1
      n = n + 1
      while n <= #lines and depth > 0 do
        local u = lines[n]:match("^%s*(.-)%s*$")
        if u:sub(1, 1) == "<" then depth = depth + 1 elseif u == ">" then depth = depth - 1 end
        out[#out + 1] = u
        n = n + 1
      end
      local nxt = lines[n] and lines[n]:match("^%s*(.-)%s*$") or ""
      if nxt:match("^PRESETNAME") then out[#out + 1] = nxt end
      return out
    end
    n = n + 1
  end
  return nil
end

local pos, done_count = 1, 0
local function step()
  if pos > #todo then
    reaper.DeleteTrack(track)
    reaper.Undo_EndBlock("Reaper to Pro Tools: export plug-in library", -1)
    reaper.ShowConsoleMsg(string.format("Finished: %d plug-ins exported to %s\n", done_count, LIB))
    return
  end
  local name = todo[pos]
  pos = pos + 1
  local sf = io.open(STATE, "a"); sf:write("current|" .. name .. "\n"); sf:close()
  local idx = reaper.TrackFX_AddByName(track, name, false, -1)
  local result = "fail"
  if idx >= 0 then
    local ok, chunk = reaper.GetTrackStateChunk(track, "", false)
    local blk = ok and block_of(chunk) or nil
    if blk then
      local f = io.open(LIB, "a")
      f:write("#PLUGIN\t" .. name .. "\n")
      for _, l in ipairs(blk) do f:write(l .. "\n") end
      f:write("#END\n")
      f:close()
      result = "done"
      done_count = done_count + 1
    end
    reaper.TrackFX_Delete(track, idx)
  end
  sf = io.open(STATE, "a"); sf:write(result .. "|" .. name .. "\n"); sf:close()
  if pos % 10 == 1 then reaper.ShowConsoleMsg(string.format("%d / %d\n", pos - 1, #todo)) end
  reaper.defer(step)
end
step()
