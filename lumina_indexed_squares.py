#! python 3
#requirements: numpy
# Inputs (Item): MapMesh Mesh, TileSize Interval (inches), Weave float, ColorCount int.
# Outputs: Geo Mesh, Outlines curves, Numbers curves.
import math
import hashlib
import ctypes
import time
import numpy as np
import Rhino
import Rhino.Geometry as rg
import scriptcontext as sc
from System.Runtime.InteropServices import GCHandle, GCHandleType
from System.Drawing import Color
from Grasshopper.Kernel import GH_RuntimeMessageLevel as Level

def report(text, error=False):
    ghenv.Component.AddRuntimeMessage(Level.Error if error else Level.Warning, text)


def unwrap(value):
    for _ in range(8):
        if hasattr(value, "Value"):
            value = value.Value
        elif isinstance(value, (list, tuple)):
            if not value:
                return None
            if len(value) != 1:
                raise ValueError("Use Item access and connect one value per input.")
            value = value[0]
        else:
            return value
    raise ValueError("Input has too many nested wrappers.")


def control(name, default, low, high):
    value = unwrap(globals().get(name))
    if value is None:
        return default
    try:
        if isinstance(value, (bool, Color)):
            raise ValueError()
        value = float(value)
        if not math.isfinite(value):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError(name + ": use one numeric slider and Item access.")
    return max(low, min(high, value))


def size_range():
    value = unwrap(globals().get("TileSize"))
    if value is None:
        value = rg.Interval(0.5, 3.0)
    if not isinstance(value, rg.Interval) or not value.IsValid:
        raise ValueError("TileSize needs Construct Domain. Set its type hint to Interval / Domain, Item access.")
    a, b = sorted((float(value.T0), float(value.T1)))
    if not all(math.isfinite(v) for v in (a, b)) or a < 0.5:
        raise ValueError("TileSize domain must start at 0.5 inches or more.")
    if b > 10000:
        raise ValueError("TileSize domain cannot exceed 10,000 inches.")
    lo = int(math.ceil(a * 2 - 1e-9))
    hi = int(math.floor(b * 2 + 1e-9))
    if lo > hi:
        raise ValueError("The domain contains no half-inch tile size.")
    if abs(lo * 0.5 - a) > 1e-8 or abs(hi * 0.5 - b) > 1e-8:
        report("Using sizes {:.1f} to {:.1f} inches, in 0.5-inch steps.".format(lo*0.5, hi*0.5))
    return lo, hi



def check_time():
    if time.perf_counter() > DEADLINE:
        raise ValueError(
            "Working-time budget exceeded. Reduce Shader Mesh resolution "
            "or process a smaller section. No partial result was output."
        )


def bulk(values, dtype, columns=1):
    # Known Rhino arrays only: Point3d[], Int32[]. No per-item fallback.
    if values.Length == 0:
        raise ValueError("Mesh array is empty.")
    size = np.dtype(dtype).itemsize * columns
    handle = GCHandle.Alloc(values, GCHandleType.Pinned)
    try:
        address = handle.AddrOfPinnedObject().ToInt64()
        if not address:
            raise ValueError("Cannot access the mesh array.")
        raw = ctypes.string_at(address, int(values.Length) * size)
    finally:
        handle.Free()
    return np.frombuffer(raw, dtype=dtype).reshape(-1, columns)


def frame(source, points, faces, step):
    # Boundary orientation preserves the map's rectangular tile axes.
    normal = None
    for ids in faces[:256, :3]:
        a, b, c = points[ids]
        n = np.cross(b-a, c-a)
        length = np.linalg.norm(n)
        if length > 0:
            normal = n / length
            break
    if normal is None:
        raise ValueError("MapMesh has no usable face.")
    edges = source.GetNakedEdges()
    if not edges:
        raise ValueError("Use an open rectangular planar mesh.")
    longest, origin, axis = 0.0, None, None
    for edge in edges:
        for i in range(edge.Count-1):
            d = edge[i+1]-edge[i]
            if d.SquareLength > longest:
                longest, origin, axis = d.SquareLength, edge[i], d
    if axis is None or not axis.Unitize():
        raise ValueError("MapMesh boundary is degenerate.")
    xa = np.array([axis.X, axis.Y, axis.Z])
    ya = np.cross(normal, xa)
    ya /= np.linalg.norm(ya)
    o = np.array([origin.X, origin.Y, origin.Z])
    d = points-o
    uv = np.column_stack((d @ xa, d @ ya))
    heights = d @ normal
    lower, upper = uv.min(axis=0), uv.max(axis=0)
    w, h = upper-lower
    tol = max(w, h, step)*1e-6
    if min(w,h) <= tol or np.ptp(heights) > tol:
        raise ValueError("MapMesh must be planar and nondegenerate.")
    for edge in edges:
        for i in range(edge.Count-1):
            a, b = edge[i], edge[i+1]
            pair = np.array([[a.X,a.Y,a.Z],[b.X,b.Y,b.Z]])-o
            p = np.column_stack((pair @ xa, pair @ ya))
            if not any(np.all(np.abs(p[:,k]-bound) <= tol)
                       for k in range(2) for bound in (lower[k],upper[k])):
                raise ValueError("MapMesh must be rectangular without holes.")
    nx = max(1, int(math.ceil(w/step-1e-10)))
    ny = max(1, int(math.ceil(h/step-1e-10)))
    if nx*ny > 16000:
        raise ValueError("More than 16,000 half-inch cells. Process sections.")
    xs = np.r_[lower[0]+np.arange(nx)*step, upper[0]]
    ys = np.r_[lower[1]+np.arange(ny)*step, upper[1]]
    yaxis = rg.Vector3d(*map(float, ya))
    plane = rg.Plane(origin, axis, yaxis)
    return dict(plane=plane, normal=rg.Vector3d(*map(float,normal)),
                xaxis=axis, yaxis=yaxis, xs=xs, ys=ys, nx=nx, ny=ny,
                z=float(heights.mean()), lift=min(step,w,h)*.002,
                step=step, tolerance=tol), uv


def locate(data, uv, faces):
    # Rasterize the exact four quarter-cell samples in arrays.
    xs, ys = data["xs"], data["ys"]
    sx = (xs[:-1,None] + np.diff(xs)[:,None]*[.25,.75]).ravel()
    sy = (ys[:-1,None] + np.diff(ys)[:,None]*[.25,.75]).ravel()
    width, height = len(sx), len(sy)
    ids = np.full((width*height,3), -1, dtype=np.int32)
    weights = np.zeros((width*height,3))
    triangles = np.concatenate((faces[:,:3], faces[faces[:,2]!=faces[:,3]][:,[0,2,3]]))
    visits = 0
    for start in range(0,len(triangles),4096):
        check_time()
        tri = triangles[start:start+4096]
        p = uv[tri]
        lo, hi = p.min(axis=1), p.max(axis=1)
        x0 = np.searchsorted(sx, lo[:,0]-1e-10)
        x1 = np.searchsorted(sx, hi[:,0]+1e-10, side="right")
        y0 = np.searchsorted(sy, lo[:,1]-1e-10)
        y1 = np.searchsorted(sy, hi[:,1]+1e-10, side="right")
        counts = (x1-x0)*(y1-y0)
        keep = counts > 0
        tri,p,x0,x1,y0,counts = [a[keep] for a in (tri,p,x0,x1,y0,counts)]
        cumulative = np.r_[0,np.cumsum(counts)]
        visits += int(cumulative[-1])
        if visits > 8000000:
            raise ValueError("Mesh triangles are too elongated for fast sampling. Remesh more uniformly.")
        # Bound every temporary candidate array.
        for first in range(0,int(cumulative[-1]),250000):
            check_time()
            seq = np.arange(first,min(first+250000,int(cumulative[-1])))
            t = np.searchsorted(cumulative[1:],seq,side="right")
            offset = seq-cumulative[t]
            cols = x0[t]+offset%(x1[t]-x0[t])
            rows = y0[t]+offset//(x1[t]-x0[t])
            a,b,c = p[t,0],p[t,1],p[t,2]
            vx,vy = b[:,0]-a[:,0],b[:,1]-a[:,1]
            wx,wy = c[:,0]-a[:,0],c[:,1]-a[:,1]
            px,py = sx[cols]-a[:,0],sy[rows]-a[:,1]
            den = vx*wy-vy*wx
            safe = np.abs(den) > 1e-30
            den = np.where(safe,den,1.0)
            u = (px*wy-py*wx)/den
            v = (vx*py-vy*px)/den
            hit = safe & (u>=-1e-9) & (v>=-1e-9) & (u+v<=1+1e-9)
            q = rows[hit]*width+cols[hit]
            # First face wins at shared edges; no double accumulation.
            q, unique = np.unique(q,return_index=True)
            pending = ids[q,0] < 0
            q, unique = q[pending],unique[pending]
            t,u,v = t[hit][unique],u[hit][unique],v[hit][unique]
            ids[q] = tri[t]
            weights[q] = np.column_stack((1-u-v,u,v))
    if np.any(ids[:,0] < 0):
        raise ValueError("Mesh has uncovered samples. Use a complete rectangular mesh.")
    return ids,weights


def base_colors(data, colors, ids, weights):
    sampled = np.einsum("ij,ijk->ik", weights, colors[ids])
    sampled = np.floor(np.clip(sampled,0,255)+.5)
    return sampled.reshape(data["ny"],2,data["nx"],2,3).mean(axis=(1,3))


def index_grid(data, base, minimum, requested, sensitivity):
    ny,nx = base.shape[:2]
    ys,xs = np.arange(0,ny,minimum),np.arange(0,nx,minimum)
    area = np.outer(np.diff(data["ys"]),np.diff(data["xs"]))
    def block_sum(a):
        return np.add.reduceat(np.add.reduceat(a,ys,axis=0),xs,axis=1)
    mass = block_sum(area)
    raw = block_sum(base*area[:,:,None])/mass[:,:,None]
    # Weave acts BEFORE indexing: higher values retain finer color variation.
    guide = raw.copy()
    if sensitivity < 1:
        padded = np.pad(raw,((1,1),(1,1),(0,0)),mode="edge")
        smooth = sum(padded[j:j+raw.shape[0],i:i+raw.shape[1]]
                     for j in range(3) for i in range(3))/9.0
        guide = sensitivity*raw+(1-sensitivity)*smooth
    values = guide.reshape(-1,3)
    actual, mass = raw.reshape(-1,3),mass.ravel()
    groups = [np.arange(len(values))]
    while len(groups) < requested:
        check_time()
        scores = [float(np.ptp(values[g],axis=0).max()*mass[g].sum())
                  if len(g)>1 else 0.0 for g in groups]
        which = int(np.argmax(scores))
        if scores[which] <= 1e-9:
            break
        g = groups.pop(which)
        axis = int(np.argmax(np.ptp(values[g],axis=0)))
        g = g[np.argsort(values[g,axis],kind="stable")]
        cumulative = np.cumsum(mass[g])
        split = min(len(g)-1,max(1,int(np.searchsorted(cumulative,cumulative[-1]*.5))+1))
        groups.extend((g[:split],g[split:]))
    centers = np.array([np.average(values[g],axis=0,weights=mass[g]) for g in groups])
    # Assign equal guide colors consistently, even across median-cut splits.
    distances = ((values[:,None,:]-centers[None,:,:])**2).sum(axis=2)
    labels = distances.argmin(axis=1)
    colors_out, active = [],[]
    for k in range(len(centers)):
        selected = labels==k
        if np.any(selected):
            colors_out.append(tuple(map(int,np.rint(np.average(actual[selected],axis=0,weights=mass[selected])))))
            active.append(k)
    entries = sorted(set(colors_out),key=lambda c:(.2126*c[0]+.7152*c[1]+.0722*c[2],c))
    lookup = np.zeros(len(centers),dtype=np.int32)
    for k,color in zip(active,colors_out):
        lookup[k] = entries.index(color)
    grid = lookup[labels].reshape(raw.shape[:2])
    grid = np.repeat(np.repeat(grid,minimum,axis=0),minimum,axis=1)[:ny,:nx]
    return entries,grid


def pack_grid(data, grid, minimum, maximum):
    ny, nx = grid.shape
    maximum = min(maximum, max(minimum, nx, ny))
    values = grid.tolist()
    bounds_cache = {}

    def square_bounds(rx, ry):
        # Largest same-index square at each anchor, allowing clipping only
        # outside the map. This prevents repeated failed size tests in detail.
        key = (rx, ry)
        if key in bounds_cache:
            return bounds_cache[key]
        bounds = [[0] * nx for _ in range(ny)]
        dx, dy = (-1 if rx else 1), (-1 if ry else 1)
        for y in (range(ny) if ry else range(ny - 1, -1, -1)):
            check_time()
            for x in (range(nx) if rx else range(nx - 1, -1, -1)):
                color = values[y][x]
                size = maximum
                for u, v in ((x + dx, y), (x, y + dy), (x + dx, y + dy)):
                    if 0 <= u < nx and 0 <= v < ny:
                        neighbor = bounds[v][u] if values[v][u] == color else 0
                        size = min(size, neighbor)
                bounds[y][x] = min(maximum, size + 1)
        bounds_cache[key] = bounds
        return bounds

    def pack(cap, increment, rx=False, ry=False, columns=False):
        bounds = square_bounds(rx, ry)
        occupied, result = [0] * ny, []
        xx = range(nx - 1, -1, -increment) if rx else range(0, nx, increment)
        yy = range(ny - 1, -1, -increment) if ry else range(0, ny, increment)
        scan = ((x, y) for x in xx for y in yy) if columns else ((x, y) for y in yy for x in xx)
        for n, (x, y) in enumerate(scan):
            if n % 256 == 0:
                check_time()
            if occupied[y] & (1 << x):
                continue
            chosen = None
            start = (min(cap, bounds[y][x]) // increment) * increment
            for k in range(start, minimum - 1, -increment):
                i, j = (x - k + 1 if rx else x), (y - k + 1 if ry else y)
                a, b, e, f = max(0, i), max(0, j), min(nx, i + k), min(ny, j + k)
                mask = ((1 << (e - a)) - 1) << a
                if any(occupied[r] & mask for r in range(b, f)):
                    continue
                chosen = (a, b, e, f, k, values[y][x])
                break
            if chosen is None:
                return None
            for r in range(b, f):
                occupied[r] |= mask
            result.append(chosen)
        return result

    # Several size caps avoid a large first tile stranding small pieces.
    # For wide domains, frequent local color-run lengths suggest useful caps.
    if maximum - minimum <= 10:
        caps = list(range(maximum, minimum - 1, -1))
    else:
        weights = {maximum: float("inf"), maximum - 1: float("inf"), minimum: float("inf")}
        for field in (grid, grid.T):
            for row in field:
                check_time()
                ends = np.flatnonzero(np.r_[True, row[1:] != row[:-1], True])
                for length in np.diff(ends):
                    length = int(length)
                    count = (length + maximum - 1) // maximum
                    cap = (length + count - 1) // count
                    if minimum <= cap <= maximum:
                        weights[cap] = weights.get(cap, 0) + length
        caps = sorted(sorted(weights, key=lambda k: (weights[k], k), reverse=True)[:10], reverse=True)

    # The minimum-size lattice always has a complete valid fallback because
    # index_grid assigns one color to each whole minimum-size block.
    plans = [(maximum, minimum, False, False, c) for c in (False, True)]
    if minimum != maximum:
        plans += [(cap, 1, False, False, c) for cap in caps for c in (False, True)]
        plans += [(maximum, 1, a, b, c) for a, b in ((True, False), (False, True), (True, True)) for c in (False, True)]
    xs, ys, step, tolerance = data["xs"], data["ys"], data["step"], data["tolerance"]
    best, best_score = None, None
    for args in dict.fromkeys(plans):
        trial = pack(*args)
        if trial is None:
            continue
        cuts = sum(abs(xs[e] - xs[a] - k * step) > tolerance or
                   abs(ys[f] - ys[b] - k * step) > tolerance
                   for a, b, e, f, k, color in trial)
        score = (len(trial), cuts)
        if best_score is None or score < best_score:
            best, best_score = trial, score
    if best is None:
        raise ValueError("Cannot construct a complete tile layout.")
    best.sort(key=lambda t: (t[1], t[0]))
    records = [((xs[a], xs[e], ys[b], ys[f]), None, 0) for a, b, e, f, k, color in best]
    return records, [t[5] for t in best]


def draw(data, records, entries, indices):
    plane, normal = data["plane"], data["normal"]
    xaxis, yaxis = data["xaxis"], data["yaxis"]
    z, lift, step = data["z"], data["lift"], data["step"]
    result = rg.Mesh()
    result.Vertices.UseDoublePrecisionVertices = True
    outlines, numbers = [], []
    index_colors = [Color.FromArgb(*c) for c in entries]
    # One PolylineCurve per digit; fit multi-digit labels as a group.
    glyphs = {
        0: [(.2, 0), (0, .2), (0, .8), (.2, 1), (.8, 1), (1, .8), (1, .2), (.8, 0), (.2, 0)],
        1: [(0, .7), (.5, 1), (.5, 0)],
        2: [(0, .8), (.2, 1), (.8, 1), (1, .8), (1, .65), (0, 0), (1, 0)],
        3: [(0, 1), (1, 1), (.45, .55), (1, .45), (1, .15), (.8, 0), (0, 0)],
        4: [(.8, 0), (.8, 1), (0, .35), (1, .35)],
        5: [(1, 1), (0, 1), (0, .55), (.8, .55), (1, .35), (1, .15), (.8, 0), (0, 0)],
        6: [(1, .9), (.8, 1), (.2, 1), (0, .75), (0, .2), (.2, 0), (.8, 0), (1, .2), (1, .45), (.8, .6), (.2, .6), (0, .4)],
        7: [(0, 1), (1, 1), (.3, 0)],
        8: [(.2, .5), (0, .7), (0, .85), (.2, 1), (.8, 1), (1, .85), (1, .7), (.8, .5), (.2, .5), (0, .3), (0, .15), (.2, 0), (.8, 0), (1, .15), (1, .3), (.8, .5)],
        9: [(1, .55), (.2, .55), (0, .75), (0, .85), (.2, 1), (.8, 1), (1, .8), (1, .2), (.8, 0), (0, 0)]
    }

    # Read upright on XY maps, regardless of boundary traversal direction.
    # For steeply tilted maps, use world Z as the text's up direction.
    horizontal = abs(normal.Z) >= 0.5
    ux, uy = (xaxis.Y, yaxis.Y) if horizontal else (xaxis.Z, yaxis.Z)
    length = math.hypot(ux, uy)
    ca, sa = uy / length, -ux / length
    handedness = -1 if horizontal and normal.Z < 0 else 1

    templates = {}

    def template(index):
        # Compose the entire label (including 10-20) before fitting it.
        strokes = []
        cursor = 0.0
        for digit in str(index):
            glyph = glyphs[int(digit)]
            u0, u1 = min(p[0] for p in glyph), max(p[0] for p in glyph)
            stroke = []
            for u, v in glyph:
                gx = handedness * (cursor + 0.62 * (u - u0))
                stroke.append((gx * ca - v * sa, gx * sa + v * ca))
            strokes.append(stroke)
            cursor += 0.62 * (u1 - u0) + 0.18
        points = [p for stroke in strokes for p in stroke]
        x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
        y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
        return strokes, x0, x1, y0, y1

    def number(index, bounds):
        left, right, bottom, top = bounds
        if index not in templates:
            templates[index] = template(index)
        strokes, x0, x1, y0, y1 = templates[index]
        scale = min(0.32 * min(right - left, top - bottom),
                    0.72 * (right - left) / (x1 - x0),
                    0.72 * (top - bottom) / (y1 - y0))
        if scale <= step * 1e-12:
            return
        cx, cy = (left + right) / 2, (bottom + top) / 2
        gx, gy = (x0 + x1) / 2, (y0 + y1) / 2
        for stroke in strokes:
            poly = rg.Polyline()
            for px, py in stroke:
                poly.Add(plane.PointAt(cx + (px - gx) * scale,
                                       cy + (py - gy) * scale, z + 4 * lift))
            numbers.append(poly.ToPolylineCurve())

    for pid, (bounds, target, area) in enumerate(records):
        if pid % 128 == 0: check_time()
        index = indices[pid]
        left,right,bottom,top = bounds
        start = result.Vertices.Count
        poly = rg.Polyline()
        for x,y in ((left,bottom),(right,bottom),(right,top),(left,top)):
            p = plane.PointAt(x,y,z)
            result.Vertices.Add(p.X,p.Y,p.Z)
            result.VertexColors.Add(index_colors[index])
            poly.Add(p)
        result.Faces.AddFace(start,start+1,start+2,start+3)
        poly.Add(poly[0])
        outlines.append(poly.ToPolylineCurve())
        number(index+1,bounds)
    result.Normals.ComputeNormals()
    result.Compact()
    return result,outlines,numbers



def build():
    global DEADLINE
    started = time.perf_counter()
    DEADLINE = started+13.0
    previous, timings = started,[]
    def lap(name):
        nonlocal previous
        now = time.perf_counter()
        timings.append("{} {:.3f}s".format(name,now-previous))
        previous = now
        if now > DEADLINE:
            print(" | ".join(timings))
        check_time()
    source = unwrap(globals().get("MapMesh"))
    if source is None:
        report("Connect one colored Shader Mesh.")
        return None,[],[]
    if not isinstance(source,rg.Mesh) or source.Faces.Count==0 or source.Vertices.Count==0:
        raise ValueError("MapMesh needs one Mesh, Item access.")
    if source.Vertices.Count != source.VertexColors.Count:
        raise ValueError("MapMesh needs a color at every vertex.")
    minimum,maximum = size_range()
    sensitivity = control("Weave",.65,0,1)
    requested = int(round(control("ColorCount",10,5,20)))
    doc = Rhino.RhinoDoc.ActiveDoc
    if doc is None or int(doc.ModelUnitSystem) in (0,11,255):
        raise ValueError("Set Rhino model units to standard physical units.")
    step = .5*Rhino.RhinoMath.UnitScale(Rhino.UnitSystem.Inches,doc.ModelUnitSystem)
    if not math.isfinite(step) or step <= 0:
        raise ValueError("Cannot convert inches to document units.")
    lap("inputs")
    points = bulk(source.Vertices.ToPoint3dArray(),np.float64,3)
    lap("read points")
    faces = bulk(source.Faces.ToIntArray(False),np.int32).reshape(-1,4)
    lap("read faces")
    packed = bulk(source.VertexColors.ToARGBArray(),np.uint32).ravel()
    lap("read colors")
    digest = hashlib.blake2b(digest_size=16)
    digest.update(points)
    digest.update(faces)
    geometry_key = (digest.digest(),float(step))
    color_key = hashlib.blake2b(packed,digest_size=16).digest()
    lap("hash")
    cache_key = ("LuminaIndexedSquares_v1",str(ghenv.Component.InstanceGuid))
    entry = sc.sticky.get(cache_key)
    if entry is None or entry["geometry_key"] != geometry_key:
        if not np.isfinite(points).all() or faces.min()<0 or faces.max()>=len(points):
            raise ValueError("Mesh has invalid coordinates or face indices.")
        data,uv = frame(source,points,faces,step)
        ids,weights = locate(data,uv,faces)
        entry = dict(geometry_key=geometry_key,data=data,ids=ids,weights=weights)
        sc.sticky[cache_key] = entry
    data = entry["data"]
    lap("locate samples")
    if entry.get("color_key") != color_key:
        if np.any((packed>>24)==0):
            raise ValueError("MapMesh contains transparent vertex colors.")
        colors = np.column_stack(((packed>>16)&255,(packed>>8)&255,packed&255)).astype(np.uint8)
        entry["base"] = base_colors(data,colors,entry["ids"],entry["weights"])
        entry["color_key"] = color_key
        entry.pop("index_key",None)
    lap("sample colors")
    index_key = (minimum,requested,sensitivity)
    if entry.get("index_key") != index_key:
        entries,grid = index_grid(data,entry["base"],minimum,requested,sensitivity)
        entry.update(index_key=index_key,entries=entries,grid=grid)
        entry.pop("layout_key",None)
    entries = entry["entries"]
    lap("index colors")
    layout_key = (minimum,maximum)
    if entry.get("layout_key") != layout_key:
        records,indices = pack_grid(data,entry["grid"],minimum,maximum)
        entry.update(layout_key=layout_key,records=records,indices=indices)
    lap("pack")
    result = draw(data,entry["records"],entries,entry["indices"])
    lap("geometry")
    print("{} tiles | {} colors | {} | total {:.3f}s".format(
        len(entry["records"]),len(entries)," | ".join(timings),time.perf_counter()-started))
    return result


Geo,Outlines,Numbers = None,[],[]
try:
    Geo,Outlines,Numbers = build()
except ValueError as exc:
    report(str(exc),True)
