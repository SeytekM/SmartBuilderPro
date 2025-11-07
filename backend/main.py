from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from shapely.geometry import Point, Polygon
from OSMPythonTools.nominatim import Nominatim
from OSMPythonTools.overpass import Overpass, overpassQueryBuilder
from cachetools import TTLCache
import math
import uuid
from datetime import datetime

# ============== ИНИЦИАЛИЗАЦИЯ ==============

app = FastAPI(
    title="SmartBuilder Pro Lite",
    version="1.0.0",
    description="Упрощённая версия без базы данных"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Хранилище данных в памяти
PROJECTS = {}
TERRITORIES = {}
ASSESSMENTS = {}

# Кеш для OSM запросов (TTL = 1 час)
osm_cache = TTLCache(maxsize=1000, ttl=3600)

# OSM сервисы
nominatim = Nominatim()
overpass = Overpass()

# ============== МОДЕЛИ ДАННЫХ ==============

class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    location: Optional[str] = None

class Project(BaseModel):
    id: str
    name: str
    description: Optional[str]
    location: Optional[str]
    created_at: str
    territories_count: int = 0

class TerritoryCreate(BaseModel):
    name: str
    project_id: str
    coordinates: List[List[List[float]]]  # GeoJSON Polygon format

class Territory(BaseModel):
    id: str
    name: str
    project_id: str
    coordinates: List[List[List[float]]]
    area_sqm: float
    centroid: Dict[str, float]
    created_at: str

class Assessment(BaseModel):
    id: str
    territory_id: str
    safety_score: float
    efficiency_score: float
    accessibility_score: float
    environmental_score: float
    overall_score: float
    recommendations: str
    metrics: Dict
    created_at: str

# ============== OSM ФУНКЦИИ ==============

def get_amenities_in_area(lat: float, lon: float, radius: int = 1000) -> Dict:
    """Получение объектов инфраструктуры"""
    cache_key = f"amenities_{lat}_{lon}_{radius}"
    
    if cache_key in osm_cache:
        return osm_cache[cache_key]
    
    amenity_types = [
        "school", "hospital", "pharmacy", "police", "fire_station",
        "bus_station", "parking", "restaurant", "cafe", "bank",
        "post_office", "library", "park", "playground"
    ]
    
    results = {}
    
    for amenity in amenity_types:
        try:
            query = overpassQueryBuilder(
                bbox=[lat - 0.01, lon - 0.01, lat + 0.01, lon + 0.01],
                elementType="node",
                selector=f'"amenity"="{amenity}"',
                out="body"
            )
            
            result = overpass.query(query, timeout=10)
            
            amenities = []
            for element in result.elements():
                amenities.append({
                    "id": element.id(),
                    "lat": element.lat(),
                    "lon": element.lon(),
                    "tags": element.tags()
                })
            
            results[amenity] = amenities
        except:
            results[amenity] = []
    
    osm_cache[cache_key] = results
    return results

def get_roads_in_polygon(coordinates: List[List[float]]) -> List[Dict]:
    """Получение дорог в полигоне"""
    try:
        lats = [coord[1] for coord in coordinates[0]]
        lons = [coord[0] for coord in coordinates[0]]
        
        bbox = [min(lats), min(lons), max(lats), max(lons)]
        
        query = overpassQueryBuilder(
            bbox=bbox,
            elementType="way",
            selector='"highway"',
            out="body"
        )
        
        result = overpass.query(query, timeout=10)
        
        roads = []
        for element in result.elements():
            roads.append({
                "id": element.id(),
                "tags": element.tags()
            })
        
        return roads
    except:
        return []

# ============== ОЦЕНКА ТЕРРИТОРИИ ==============

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах"""
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    
    a = (math.sin(delta_phi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c

def find_closest(point: Point, amenities: List[Dict]) -> Optional[float]:
    """Найти ближайший объект"""
    if not amenities:
        return None
    
    min_dist = float('inf')
    for amenity in amenities:
        dist = haversine_distance(point.y, point.x, amenity["lat"], amenity["lon"])
        min_dist = min(min_dist, dist)
    
    return min_dist if min_dist != float('inf') else None

def calculate_safety_score(centroid: Point, amenities: Dict) -> float:
    """Оценка безопасности"""
    score = 50.0
    
    if amenities.get("police"):
        dist = find_closest(centroid, amenities["police"])
        if dist and dist < 2000:
            score += 20
        elif dist and dist < 5000:
            score += 10
    
    if amenities.get("fire_station"):
        dist = find_closest(centroid, amenities["fire_station"])
        if dist and dist < 3000:
            score += 15
        elif dist and dist < 7000:
            score += 7
    
    if amenities.get("hospital"):
        dist = find_closest(centroid, amenities["hospital"])
        if dist and dist < 3000:
            score += 15
        elif dist and dist < 5000:
            score += 10
    
    return min(score, 100.0)

def calculate_efficiency_score(roads: List[Dict], area_sqm: float, amenities: Dict) -> float:
    """Оценка эффективности"""
    score = 40.0
    
    road_density = len(roads) / (area_sqm / 1000000)
    if road_density > 10:
        score += 20
    elif road_density > 5:
        score += 15
    elif road_density > 2:
        score += 10
    
    if amenities.get("bus_station"):
        score += 15
    
    parking_count = len(amenities.get("parking", []))
    if parking_count > 2:
        score += 10
    elif parking_count > 0:
        score += 5
    
    commerce = (len(amenities.get("restaurant", [])) +
                len(amenities.get("cafe", [])) +
                len(amenities.get("bank", [])))
    if commerce > 10:
        score += 15
    elif commerce > 5:
        score += 10
    
    return min(score, 100.0)

def calculate_accessibility_score(centroid: Point, amenities: Dict) -> float:
    """Оценка доступности"""
    score = 30.0
    
    services = {
        "school": (1000, 20),
        "hospital": (3000, 15),
        "pharmacy": (500, 10),
        "park": (1000, 10),
        "post_office": (2000, 5),
        "library": (2000, 5)
    }
    
    for service, (max_dist, points) in services.items():
        if amenities.get(service):
            dist = find_closest(centroid, amenities[service])
            if dist and dist < max_dist:
                score += points
            elif dist and dist < max_dist * 2:
                score += points / 2
    
    return min(score, 100.0)

def calculate_environmental_score(amenities: Dict) -> float:
    """Оценка экологичности"""
    score = 50.0
    
    parks_count = len(amenities.get("park", [])) + len(amenities.get("playground", []))
    if parks_count > 5:
        score += 25
    elif parks_count > 2:
        score += 15
    elif parks_count > 0:
        score += 10
    
    return min(score, 100.0)

def generate_recommendations(safety: float, efficiency: float, accessibility: float, environmental: float, amenities: Dict) -> str:
    """Генерация рекомендаций"""
    recs = []
    
    if safety < 60:
        recs.append("• Улучшить инфраструктуру безопасности (полиция, пожарные станции)")
    
    if efficiency < 60:
        recs.append("• Развить дорожную сеть и общественный транспорт")
    
    if accessibility < 60:
        missing = []
        if not amenities.get("school"):
            missing.append("школы")
        if not amenities.get("hospital"):
            missing.append("медучреждения")
        if not amenities.get("park"):
            missing.append("парки")
        
        if missing:
            recs.append(f"• Добавить: {', '.join(missing)}")
    
    if environmental < 60:
        recs.append("• Увеличить количество зеленых зон")
    
    if not recs:
        recs.append("• Территория хорошо развита. Поддерживайте текущее состояние.")
    
    return "\n".join(recs)

# ============== API ENDPOINTS ==============

@app.get("/")
async def root():
    return {
        "app": "SmartBuilder Pro Lite",
        "version": "1.0.0",
        "status": "running",
        "projects": len(PROJECTS),
        "territories": len(TERRITORIES)
    }

@app.post("/projects", response_model=Project)
async def create_project(project_data: ProjectCreate):
    """Создание проекта"""
    project_id = str(uuid.uuid4())
    
    project = {
        "id": project_id,
        "name": project_data.name,
        "description": project_data.description,
        "location": project_data.location,
        "created_at": datetime.now().isoformat(),
        "territories_count": 0
    }
    
    PROJECTS[project_id] = project
    return Project(**project)

@app.get("/projects", response_model=List[Project])
async def get_projects():
    """Получение всех проектов"""
    return [Project(**p) for p in PROJECTS.values()]

@app.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str):
    """Получение проекта"""
    if project_id not in PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")
    return Project(**PROJECTS[project_id])

@app.post("/territories", response_model=Territory)
async def create_territory(territory_data: TerritoryCreate):
    """Создание территории с автоматической оценкой"""
    
    if territory_data.project_id not in PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")
    
    territory_id = str(uuid.uuid4())
    
    # Создание полигона
    polygon = Polygon(territory_data.coordinates[0])
    area_sqm = polygon.area * 111320 * 111320
    centroid = polygon.centroid
    
    # Сохранение территории
    territory = {
        "id": territory_id,
        "name": territory_data.name,
        "project_id": territory_data.project_id,
        "coordinates": territory_data.coordinates,
        "area_sqm": area_sqm,
        "centroid": {"lat": centroid.y, "lon": centroid.x},
        "created_at": datetime.now().isoformat()
    }
    
    TERRITORIES[territory_id] = territory
    PROJECTS[territory_data.project_id]["territories_count"] += 1
    
    # Получение данных OSM и оценка
    try:
        amenities = get_amenities_in_area(centroid.y, centroid.x)
        roads = get_roads_in_polygon(territory_data.coordinates)
        
        safety = calculate_safety_score(centroid, amenities)
        efficiency = calculate_efficiency_score(roads, area_sqm, amenities)
        accessibility = calculate_accessibility_score(centroid, amenities)
        environmental = calculate_environmental_score(amenities)
        overall = (safety + efficiency + accessibility + environmental) / 4
        
        recommendations = generate_recommendations(safety, efficiency, accessibility, environmental, amenities)
        
        assessment = {
            "id": str(uuid.uuid4()),
            "territory_id": territory_id,
            "safety_score": round(safety, 2),
            "efficiency_score": round(efficiency, 2),
            "accessibility_score": round(accessibility, 2),
            "environmental_score": round(environmental, 2),
            "overall_score": round(overall, 2),
            "recommendations": recommendations,
            "metrics": {
                "amenities_count": {k: len(v) for k, v in amenities.items()},
                "roads_count": len(roads)
            },
            "created_at": datetime.now().isoformat()
        }
        
        ASSESSMENTS[territory_id] = assessment
    except Exception as e:
        print(f"Error during assessment: {e}")
    
    return Territory(**territory)

@app.get("/territories/{territory_id}", response_model=Territory)
async def get_territory(territory_id: str):
    """Получение территории"""
    if territory_id not in TERRITORIES:
        raise HTTPException(status_code=404, detail="Territory not found")
    return Territory(**TERRITORIES[territory_id])

@app.get("/assessments/{territory_id}", response_model=Assessment)
async def get_assessment(territory_id: str):
    """Получение оценки территории"""
    if territory_id not in ASSESSMENTS:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return Assessment(**ASSESSMENTS[territory_id])

@app.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """Удаление проекта"""
    if project_id not in PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Удаление всех территорий проекта
    territories_to_delete = [t_id for t_id, t in TERRITORIES.items() if t["project_id"] == project_id]
    for t_id in territories_to_delete:
        del TERRITORIES[t_id]
        if t_id in ASSESSMENTS:
            del ASSESSMENTS[t_id]
    
    del PROJECTS[project_id]
    return {"message": "Project deleted"}

@app.get("/stats")
async def get_stats():
    """Статистика системы"""
    return {
        "projects": len(PROJECTS),
        "territories": len(TERRITORIES),
        "assessments": len(ASSESSMENTS),
        "cache_size": len(osm_cache)
    }
