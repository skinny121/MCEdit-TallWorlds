'''
Created on Jul 22, 2011

@author: Rio
'''
import collections

from datetime import datetime
import itertools
from logging import getLogger
from math import floor
import os
import random
import shutil
import struct
import time
import traceback
import weakref
import zlib
import sys

from box import BoundingBox
from entity import Entity, TileEntity, TileTick
from faces import FaceXDecreasing, FaceXIncreasing, FaceZDecreasing, FaceZIncreasing
from level import LightedChunk, EntityLevel, computeChunkHeightMap, MCLevel, ChunkBase
from materials import alphaMaterials
from mclevelbase import ChunkMalformed, ChunkNotPresent, ChunkAccessDenied,ChunkConcurrentException,exhaust, PlayerNotFound
import nbt
from numpy import array, clip, maximum, zeros
from regionfile import MCRegionFile
from pc_metadata import PCMetadata, SessionLockLost
import logging
from uuid import UUID

log = getLogger(__name__)

DIM_NETHER = -1
DIM_END = 1

__all__ = ["ZeroChunk", "AnvilChunk", "ChunkedLevelMixin", "MCInfdevOldLevel", "MCAlphaDimension"]
_zeros = {}

def ZeroChunk(height=512):
    z = _zeros.get(height)
    if z is None:
        z = _zeros[height] = _ZeroChunk(height)
    return z


class _ZeroChunk(ChunkBase):
    " a placebo for neighboring-chunk routines "

    def __init__(self, height=512):
        zeroChunk = zeros((16, 16, height), 'uint8')
        whiteLight = zeroChunk + 15
        self.Blocks = zeroChunk
        self.BlockLight = whiteLight
        self.SkyLight = whiteLight
        self.Data = zeroChunk


def unpackNibbleArray(dataArray):
    s = dataArray.shape
    unpackedData = zeros((s[0], s[1], s[2] * 2), dtype='uint8')

    unpackedData[:, :, ::2] = dataArray
    unpackedData[:, :, ::2] &= 0xf
    unpackedData[:, :, 1::2] = dataArray
    unpackedData[:, :, 1::2] >>= 4
    return unpackedData


def packNibbleArray(unpackedData):
    packedData = array(unpackedData.reshape(16, 16, unpackedData.shape[2] / 2, 2))
    packedData[..., 1] <<= 4
    packedData[..., 1] |= packedData[..., 0]
    return array(packedData[:, :, :, 1])


def sanitizeBlocks(chunk):
    # change grass to dirt where needed so Minecraft doesn't flip out and die
    grass = chunk.Blocks == chunk.materials.Grass.ID
    grass |= chunk.Blocks == chunk.materials.Dirt.ID
    badgrass = grass[:, :, 1:] & grass[:, :, :-1]

    chunk.Blocks[:, :, :-1][badgrass] = chunk.materials.Dirt.ID

    # remove any thin snow layers immediately above other thin snow layers.
    # minecraft doesn't flip out, but it's almost never intended
    if hasattr(chunk.materials, "SnowLayer"):
        snowlayer = chunk.Blocks == chunk.materials.SnowLayer.ID
        badsnow = snowlayer[:, :, 1:] & snowlayer[:, :, :-1]

        chunk.Blocks[:, :, 1:][badsnow] = chunk.materials.Air.ID


class AnvilChunkData(object):
    """ This is the chunk data backing an AnvilChunk. Chunk data is retained by the MCInfdevOldLevel until its
    AnvilChunk is no longer used, then it is either cached in memory, discarded, or written to disk according to
    resource limits.

    AnvilChunks are stored in a WeakValueDictionary so we can find out when they are no longer used by clients. The
    AnvilChunkData for an unused chunk may safely be discarded or written out to disk. The client should probably
     not keep references to a whole lot of chunks or else it will run out of memory.
    """

    def __init__(self, world, chunkPosition, root_tag=None, create=False):
        self.chunkPosition = chunkPosition
        self.world = world
        self.root_tag = root_tag
        self.dirty = False

        self.Blocks = zeros((16, 16, world.Height), 'uint16')
        self.Data = zeros((16, 16, world.Height), 'uint8')
        self.BlockLight = zeros((16, 16, world.Height), 'uint8')
        self.SkyLight = zeros((16, 16, world.Height), 'uint8')
        self.SkyLight[:] = 15

        if create:
            self._create()
        else:
            self._load(root_tag)

        levelTag = self.root_tag["Level"]
        if "Biomes" not in levelTag:
            levelTag["Biomes"] = nbt.TAG_Byte_Array(zeros((16, 16), 'uint8'))
            levelTag["Biomes"].value[:] = -1

    def _create(self):
        (cx, cz) = self.chunkPosition
        chunkTag = nbt.TAG_Compound()
        chunkTag.name = ""

        levelTag = nbt.TAG_Compound()
        chunkTag["Level"] = levelTag

        levelTag["HeightMap"] = nbt.TAG_Int_Array(zeros((16, 16), 'uint32').newbyteorder())
        levelTag["TerrainPopulated"] = nbt.TAG_Byte(1)
        levelTag["xPos"] = nbt.TAG_Int(cx)
        levelTag["zPos"] = nbt.TAG_Int(cz)

        levelTag["LastUpdate"] = nbt.TAG_Long(0)

        levelTag["Entities"] = nbt.TAG_List()
        levelTag["TileEntities"] = nbt.TAG_List()
        levelTag["TileTicks"] = nbt.TAG_List()

        self.root_tag = chunkTag

        self.dirty = True

    def _load(self, root_tag):
        self.root_tag = root_tag

        for sec in self.root_tag["Level"].pop("Sections", []):
            y = sec["Y"].value * 16

            for name in "Blocks", "Data", "SkyLight", "BlockLight":
                arr = getattr(self, name)
                secarray = sec[name].value
                if name == "Blocks":
                    secarray.shape = (16, 16, 16)
                else:
                    secarray.shape = (16, 16, 8)
                    secarray = unpackNibbleArray(secarray)

                arr[..., y:y + 16] = secarray.swapaxes(0, 2)

            tag = sec.get("Add")
            if tag is not None:
                tag.value.shape = (16, 16, 8)
                add = unpackNibbleArray(tag.value)
                self.Blocks[..., y:y + 16] |= (array(add, 'uint16') << 8).swapaxes(0, 2)

    def savedTagData(self):
        """ does not recalculate any data or light """

        log.debug(u"Saving chunk: {0}".format(self))
        sanitizeBlocks(self)

        sections = nbt.TAG_List()
        for y in range(0, self.world.Height, 16):
            section = nbt.TAG_Compound()

            Blocks = self.Blocks[..., y:y + 16].swapaxes(0, 2)
            Data = self.Data[..., y:y + 16].swapaxes(0, 2)
            BlockLight = self.BlockLight[..., y:y + 16].swapaxes(0, 2)
            SkyLight = self.SkyLight[..., y:y + 16].swapaxes(0, 2)

            if (not Blocks.any() and
                    not BlockLight.any() and
                    (SkyLight == 15).all()):
                continue

            Data = packNibbleArray(Data)
            BlockLight = packNibbleArray(BlockLight)
            SkyLight = packNibbleArray(SkyLight)

            add = Blocks >> 8
            if add.any():
                section["Add"] = nbt.TAG_Byte_Array(packNibbleArray(add).astype('uint8'))

            section['Blocks'] = nbt.TAG_Byte_Array(array(Blocks, 'uint8'))
            section['Data'] = nbt.TAG_Byte_Array(array(Data))
            section['BlockLight'] = nbt.TAG_Byte_Array(array(BlockLight))
            section['SkyLight'] = nbt.TAG_Byte_Array(array(SkyLight))

            section["Y"] = nbt.TAG_Byte(y / 16)
            sections.append(section)

        self.root_tag["Level"]["Sections"] = sections
        data = self.root_tag.save(compressed=False)
        del self.root_tag["Level"]["Sections"]

        log.debug(u"Saved chunk {0}".format(self))
        return data

    @property
    def materials(self):
        return self.world.materials


class AnvilChunk(LightedChunk):
    """ This is a 16x16xH chunk in an (infinite) world.
    The properties Blocks, Data, SkyLight, BlockLight, and Heightmap
    are ndarrays containing the respective blocks in the chunk file.
    Each array is indexed [x,z,y].  The Data, Skylight, and BlockLight
    arrays are automatically unpacked from nibble arrays into byte arrays
    for better handling.
    """

    def __init__(self, chunkData):
        self.world = chunkData.world
        self.chunkPosition = chunkData.chunkPosition
        self.chunkData = chunkData

    def savedTagData(self):
        return self.chunkData.savedTagData()

    def __str__(self):
        return u"AnvilChunk, coords:{0}, world: {1}, D:{2}, L:{3}".format(self.chunkPosition, self.world.displayName,
                                                                          self.dirty, self.needsLighting)

    @property
    def needsLighting(self):
        return self.chunkPosition in self.world.chunksNeedingLighting

    @needsLighting.setter
    def needsLighting(self, value):
        if value:
            self.world.chunksNeedingLighting.add(self.chunkPosition)
        else:
            self.world.chunksNeedingLighting.discard(self.chunkPosition)

    def generateHeightMap(self):
        computeChunkHeightMap(self.materials, self.Blocks, self.HeightMap)

    def addEntity(self, entityTag):

        def doubleize(name):
            # This is needed for compatibility with Indev levels. Those levels use TAG_Float for entity motion and pos
            if name in entityTag:
                m = entityTag[name]
                entityTag[name] = nbt.TAG_List([nbt.TAG_Double(i.value) for i in m])

        doubleize("Motion")
        doubleize("Position")

        self.dirty = True
        return super(AnvilChunk, self).addEntity(entityTag)

    def removeEntitiesInBox(self, box):
        self.dirty = True
        return super(AnvilChunk, self).removeEntitiesInBox(box)

    def removeTileEntitiesInBox(self, box):
        self.dirty = True
        return super(AnvilChunk, self).removeTileEntitiesInBox(box)

    def addTileTick(self, tickTag):
        self.dirty = True
        return super(AnvilChunk, self).addTileTick(tickTag)

    def removeTileTicksInBox(self, box):
        self.dirty = True
        return super(AnvilChunk, self).removeTileTicksInBox(box)

    # --- AnvilChunkData accessors ---

    @property
    def root_tag(self):
        return self.chunkData.root_tag

    @property
    def dirty(self):
        return self.chunkData.dirty

    @dirty.setter
    def dirty(self, val):
        self.chunkData.dirty = val

    # --- Chunk attributes ---

    @property
    def materials(self):
        return self.world.materials

    @property
    def Blocks(self):
        return self.chunkData.Blocks

    @Blocks.setter
    def Blocks(self, value):
        self.chunkData.Blocks = value

    @property
    def Data(self):
        return self.chunkData.Data

    @property
    def SkyLight(self):
        return self.chunkData.SkyLight

    @property
    def BlockLight(self):
        return self.chunkData.BlockLight

    @property
    def Biomes(self):
        return self.root_tag["Level"]["Biomes"].value.reshape((16, 16))

    @property
    def HeightMap(self):
        return self.root_tag["Level"]["HeightMap"].value.reshape((16, 16))

    @property
    def Entities(self):
        return self.root_tag["Level"]["Entities"]

    @property
    def TileEntities(self):
        return self.root_tag["Level"]["TileEntities"]

    @property
    def TileTicks(self):
        if "TileTicks" in self.root_tag["Level"]:
            return self.root_tag["Level"]["TileTicks"]
        else:
            self.root_tag["Level"]["TileTicks"] = nbt.TAG_List()
            return self.root_tag["Level"]["TileTicks"]

    @property
    def TerrainPopulated(self):
        return self.root_tag["Level"]["TerrainPopulated"].value

    @TerrainPopulated.setter
    def TerrainPopulated(self, val):
        """True or False. If False, the game will populate the chunk with
        ores and vegetation on next load"""
        self.root_tag["Level"]["TerrainPopulated"].value = val
        self.dirty = True


base36alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"


def decbase36(s):
    return int(s, 36)


def base36(n):
    global base36alphabet

    n = int(n)
    if 0 == n:
        return '0'
    neg = ""
    if n < 0:
        neg = "-"
        n = -n

    work = []

    while n:
        n, digit = divmod(n, 36)
        work.append(base36alphabet[digit])

    return neg + ''.join(reversed(work))


def deflate(data):
    # zobj = zlib.compressobj(6,zlib.DEFLATED,-zlib.MAX_WBITS,zlib.DEF_MEM_LEVEL,0)
    # zdata = zobj.compress(data)
    # zdata += zobj.flush()
    # return zdata
    return zlib.compress(data)


def inflate(data):
    return zlib.decompress(data)


class ChunkedLevelMixin(MCLevel):
    def blockLightAt(self, x, y, z):
        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf
        ch = self.getChunk(xc, zc)

        return ch.BlockLight[xInChunk, zInChunk, y]

    def setBlockLightAt(self, x, y, z, newLight):
        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf

        ch = self.getChunk(xc, zc)
        ch.BlockLight[xInChunk, zInChunk, y] = newLight
        ch.chunkChanged(False)

    def blockDataAt(self, x, y, z):
        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf

        try:
            ch = self.getChunk(xc, zc)
        except ChunkNotPresent:
            return 0

        return ch.Data[xInChunk, zInChunk, y]

    def setBlockDataAt(self, x, y, z, newdata):
        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf

        try:
            ch = self.getChunk(xc, zc)
        except ChunkNotPresent:
            return 0

        ch.Data[xInChunk, zInChunk, y] = newdata
        ch.dirty = True
        ch.needsLighting = True

    def blockAt(self, x, y, z):
        """returns 0 for blocks outside the loadable chunks.  automatically loads chunks."""
        if y < 0 or y >= self.Height:
            return 0

        zc = z >> 4
        xc = x >> 4
        xInChunk = x & 0xf
        zInChunk = z & 0xf

        try:
            ch = self.getChunk(xc, zc)
        except ChunkNotPresent:
            return 0

        return ch.Blocks[xInChunk, zInChunk, y]

    def setBlockAt(self, x, y, z, blockID):
        """returns 0 for blocks outside the loadable chunks.  automatically loads chunks."""
        if y < 0 or y >= self.Height:
            return 0

        zc = z >> 4
        xc = x >> 4
        xInChunk = x & 0xf
        zInChunk = z & 0xf

        try:
            ch = self.getChunk(xc, zc)
        except ChunkNotPresent:
            return 0

        ch.Blocks[xInChunk, zInChunk, y] = blockID
        ch.dirty = True
        ch.needsLighting = True

    def skylightAt(self, x, y, z):

        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf

        ch = self.getChunk(xc, zc)

        return ch.SkyLight[xInChunk, zInChunk, y]

    def setSkylightAt(self, x, y, z, lightValue):
        if y < 0 or y >= self.Height:
            return 0
        zc = z >> 4
        xc = x >> 4

        xInChunk = x & 0xf
        zInChunk = z & 0xf

        ch = self.getChunk(xc, zc)
        skyLight = ch.SkyLight

        oldValue = skyLight[xInChunk, zInChunk, y]

        ch.chunkChanged(False)
        if oldValue < lightValue:
            skyLight[xInChunk, zInChunk, y] = lightValue
        return oldValue < lightValue

    createChunk = NotImplemented

    def generateLights(self, dirtyChunkPositions=None):
        return exhaust(self.generateLightsIter(dirtyChunkPositions))

    def generateLightsIter(self, dirtyChunkPositions=None):
        """ dirtyChunks may be an iterable yielding (xPos,zPos) tuples
        if none, generate lights for all chunks that need lighting
        """

        startTime = datetime.now()

        if dirtyChunkPositions is None:
            dirtyChunkPositions = self.chunksNeedingLighting
        else:
            dirtyChunkPositions = (c for c in dirtyChunkPositions if self.containsChunk(*c))

        dirtyChunkPositions = sorted(dirtyChunkPositions)

        maxLightingChunks = getattr(self, 'loadedChunkLimit', 400)

        log.info(u"Asked to light {0} chunks".format(len(dirtyChunkPositions)))
        chunkLists = [dirtyChunkPositions]

        def reverseChunkPosition((cx, cz)):
            return cz, cx

        def splitChunkLists(chunkLists):
            newChunkLists = []
            for l in chunkLists:
                # list is already sorted on x position, so this splits into left and right

                smallX = l[:len(l) / 2]
                bigX = l[len(l) / 2:]

                # sort halves on z position
                smallX = sorted(smallX, key=reverseChunkPosition)
                bigX = sorted(bigX, key=reverseChunkPosition)

                # add quarters to list

                newChunkLists.append(smallX[:len(smallX) / 2])
                newChunkLists.append(smallX[len(smallX) / 2:])

                newChunkLists.append(bigX[:len(bigX) / 2])
                newChunkLists.append(bigX[len(bigX) / 2:])

            return newChunkLists

        while len(chunkLists[0]) > maxLightingChunks:
            chunkLists = splitChunkLists(chunkLists)

        if len(chunkLists) > 1:
            log.info(u"Using {0} batches to conserve memory.".format(len(chunkLists)))
        # batchSize = min(len(a) for a in chunkLists)
        estimatedTotals = [len(a) * 32 for a in chunkLists]
        workDone = 0

        for i, dc in enumerate(chunkLists):
            log.info(u"Batch {0}/{1}".format(i, len(chunkLists)))

            dc = sorted(dc)
            workTotal = sum(estimatedTotals)
            t = 0
            for c, t, p in self._generateLightsIter(dc):
                yield c + workDone, t + workTotal - estimatedTotals[i], p

            estimatedTotals[i] = t
            workDone += t

        timeDelta = datetime.now() - startTime

        if len(dirtyChunkPositions):
            log.info(u"Completed in {0}, {1} per chunk".format(timeDelta, dirtyChunkPositions and timeDelta / len(
                dirtyChunkPositions) or 0))

        return

    def _generateLightsIter(self, dirtyChunkPositions):
        la = array(self.materials.lightAbsorption)
        clip(la, 1, 15, la)

        dirtyChunks = set(self.getChunk(*cPos) for cPos in dirtyChunkPositions)

        workDone = 0
        workTotal = len(dirtyChunks) * 29

        progressInfo = (u"Lighting {0} chunks".format(len(dirtyChunks)))
        log.info(progressInfo)

        for i, chunk in enumerate(dirtyChunks):
            chunk.chunkChanged()
            yield i, workTotal, progressInfo
            assert chunk.dirty and chunk.needsLighting

        workDone += len(dirtyChunks)
        workTotal = len(dirtyChunks)

        for ch in list(dirtyChunks):
            # relight all blocks in neighboring chunks in case their light source disappeared.
            cx, cz = ch.chunkPosition
            for dx, dz in itertools.product((-1, 0, 1), (-1, 0, 1)):
                try:
                    ch = self.getChunk(cx + dx, cz + dz)
                except (ChunkNotPresent, ChunkMalformed):
                    continue
                dirtyChunks.add(ch)
                ch.dirty = True

        dirtyChunks = sorted(dirtyChunks, key=lambda x: x.chunkPosition)
        workTotal += len(dirtyChunks) * 28

        for i, chunk in enumerate(dirtyChunks):
            chunk.BlockLight[:] = self.materials.lightEmission[chunk.Blocks]
            chunk.dirty = True

        zeroChunk = ZeroChunk(self.Height)
        zeroChunk.BlockLight[:] = 0
        zeroChunk.SkyLight[:] = 0

        startingDirtyChunks = dirtyChunks

        oldLeftEdge = zeros((1, 16, self.Height), 'uint8')
        oldBottomEdge = zeros((16, 1, self.Height), 'uint8')
        oldChunk = zeros((16, 16, self.Height), 'uint8')
        if self.dimNo in (-1, 1):
            lights = ("BlockLight",)
        else:
            lights = ("BlockLight", "SkyLight")
        log.info(u"Dispersing light...")

        def clipLight(light):
            # light arrays are all uint8 by default, so when results go negative
            # they become large instead.  reinterpret as signed int using view()
            # and then clip to range
            light.view('int8').clip(0, 15, light)

        for j, light in enumerate(lights):
            zerochunkLight = getattr(zeroChunk, light)
            newDirtyChunks = list(startingDirtyChunks)

            work = 0

            for i in range(14):
                if len(newDirtyChunks) == 0:
                    workTotal -= len(startingDirtyChunks) * (14 - i)
                    break

                progressInfo = u"{0} Pass {1}: {2} chunks".format(light, i, len(newDirtyChunks))
                log.info(progressInfo)

                # propagate light!
                #                for each of the six cardinal directions, figure a new light value for
                #                adjoining blocks by reducing this chunk's light by light absorption and fall off.
                #                compare this new light value against the old light value and update with the maximum.
                #
                #                we calculate all chunks one step before moving to the next step, to ensure all gaps at chunk edges are filled.
                #                we do an extra cycle because lights sent across edges may lag by one cycle.
                #
                #                xxx this can be optimized by finding the highest and lowest blocks
                #                that changed after one pass, and only calculating changes for that
                #                vertical slice on the next pass. newDirtyChunks would have to be a
                #                list of (cPos, miny, maxy) tuples or a cPos : (miny, maxy) dict

                newDirtyChunks = set(newDirtyChunks)
                newDirtyChunks.discard(zeroChunk)

                dirtyChunks = sorted(newDirtyChunks, key=lambda x: x.chunkPosition)

                newDirtyChunks = list()

                for chunk in dirtyChunks:
                    (cx, cz) = chunk.chunkPosition
                    neighboringChunks = {}

                    for dir, dx, dz in ((FaceXDecreasing, -1, 0),
                                        (FaceXIncreasing, 1, 0),
                                        (FaceZDecreasing, 0, -1),
                                        (FaceZIncreasing, 0, 1)):
                        try:
                            neighboringChunks[dir] = self.getChunk(cx + dx, cz + dz)
                        except (ChunkNotPresent, ChunkMalformed):
                            neighboringChunks[dir] = zeroChunk
                        neighboringChunks[dir].dirty = True

                    chunkLa = la[chunk.Blocks]
                    chunkLight = getattr(chunk, light)
                    oldChunk[:] = chunkLight[:]

                    ### Spread light toward -X

                    nc = neighboringChunks[FaceXDecreasing]
                    ncLight = getattr(nc, light)
                    oldLeftEdge[:] = ncLight[15:16, :, 0:self.Height]  # save the old left edge

                    # left edge
                    newlight = (chunkLight[0:1, :, :self.Height] - la[nc.Blocks[15:16, :, 0:self.Height]])
                    clipLight(newlight)

                    maximum(ncLight[15:16, :, 0:self.Height], newlight, ncLight[15:16, :, 0:self.Height])

                    # chunk body
                    newlight = (chunkLight[1:16, :, 0:self.Height] - chunkLa[0:15, :, 0:self.Height])
                    clipLight(newlight)

                    maximum(chunkLight[0:15, :, 0:self.Height], newlight, chunkLight[0:15, :, 0:self.Height])

                    # right edge
                    nc = neighboringChunks[FaceXIncreasing]
                    ncLight = getattr(nc, light)

                    newlight = ncLight[0:1, :, :self.Height] - chunkLa[15:16, :, 0:self.Height]
                    clipLight(newlight)

                    maximum(chunkLight[15:16, :, 0:self.Height], newlight, chunkLight[15:16, :, 0:self.Height])

                    ### Spread light toward +X

                    # right edge
                    nc = neighboringChunks[FaceXIncreasing]
                    ncLight = getattr(nc, light)

                    newlight = (chunkLight[15:16, :, 0:self.Height] - la[nc.Blocks[0:1, :, 0:self.Height]])
                    clipLight(newlight)

                    maximum(ncLight[0:1, :, 0:self.Height], newlight, ncLight[0:1, :, 0:self.Height])

                    # chunk body
                    newlight = (chunkLight[0:15, :, 0:self.Height] - chunkLa[1:16, :, 0:self.Height])
                    clipLight(newlight)

                    maximum(chunkLight[1:16, :, 0:self.Height], newlight, chunkLight[1:16, :, 0:self.Height])

                    # left edge
                    nc = neighboringChunks[FaceXDecreasing]
                    ncLight = getattr(nc, light)

                    newlight = ncLight[15:16, :, :self.Height] - chunkLa[0:1, :, 0:self.Height]
                    clipLight(newlight)

                    maximum(chunkLight[0:1, :, 0:self.Height], newlight, chunkLight[0:1, :, 0:self.Height])

                    zerochunkLight[:] = 0  # zero the zero chunk after each direction
                    # so the lights it absorbed don't affect the next pass

                    # check if the left edge changed and dirty or compress the chunk appropriately
                    if (oldLeftEdge != ncLight[15:16, :, :self.Height]).any():
                        # chunk is dirty
                        newDirtyChunks.append(nc)

                    ### Spread light toward -Z

                    # bottom edge
                    nc = neighboringChunks[FaceZDecreasing]
                    ncLight = getattr(nc, light)
                    oldBottomEdge[:] = ncLight[:, 15:16, :self.Height]  # save the old bottom edge

                    newlight = (chunkLight[:, 0:1, :self.Height] - la[nc.Blocks[:, 15:16, :self.Height]])
                    clipLight(newlight)

                    maximum(ncLight[:, 15:16, :self.Height], newlight, ncLight[:, 15:16, :self.Height])

                    # chunk body
                    newlight = (chunkLight[:, 1:16, :self.Height] - chunkLa[:, 0:15, :self.Height])
                    clipLight(newlight)

                    maximum(chunkLight[:, 0:15, :self.Height], newlight, chunkLight[:, 0:15, :self.Height])

                    # top edge
                    nc = neighboringChunks[FaceZIncreasing]
                    ncLight = getattr(nc, light)

                    newlight = ncLight[:, 0:1, :self.Height] - chunkLa[:, 15:16, 0:self.Height]
                    clipLight(newlight)

                    maximum(chunkLight[:, 15:16, 0:self.Height], newlight, chunkLight[:, 15:16, 0:self.Height])

                    ### Spread light toward +Z

                    # top edge
                    nc = neighboringChunks[FaceZIncreasing]

                    ncLight = getattr(nc, light)

                    newlight = (chunkLight[:, 15:16, :self.Height] - la[nc.Blocks[:, 0:1, :self.Height]])
                    clipLight(newlight)

                    maximum(ncLight[:, 0:1, :self.Height], newlight, ncLight[:, 0:1, :self.Height])

                    # chunk body
                    newlight = (chunkLight[:, 0:15, :self.Height] - chunkLa[:, 1:16, :self.Height])
                    clipLight(newlight)

                    maximum(chunkLight[:, 1:16, :self.Height], newlight, chunkLight[:, 1:16, :self.Height])

                    # bottom edge
                    nc = neighboringChunks[FaceZDecreasing]
                    ncLight = getattr(nc, light)

                    newlight = ncLight[:, 15:16, :self.Height] - chunkLa[:, 0:1, 0:self.Height]
                    clipLight(newlight)

                    maximum(chunkLight[:, 0:1, 0:self.Height], newlight, chunkLight[:, 0:1, 0:self.Height])

                    zerochunkLight[:] = 0

                    if (oldBottomEdge != ncLight[:, 15:16, :self.Height]).any():
                        newDirtyChunks.append(nc)

                    newlight = (chunkLight[:, :, 0:self.Height - 1] - chunkLa[:, :, 1:self.Height])
                    clipLight(newlight)
                    maximum(chunkLight[:, :, 1:self.Height], newlight, chunkLight[:, :, 1:self.Height])

                    newlight = (chunkLight[:, :, 1:self.Height] - chunkLa[:, :, 0:self.Height - 1])
                    clipLight(newlight)
                    maximum(chunkLight[:, :, 0:self.Height - 1], newlight, chunkLight[:, :, 0:self.Height - 1])

                    if (oldChunk != chunkLight).any():
                        newDirtyChunks.append(chunk)

                    work += 1
                    yield workDone + work, workTotal, progressInfo

                workDone += work
                workTotal -= len(startingDirtyChunks)
                workTotal += work

                work = 0

        for ch in startingDirtyChunks:
            ch.needsLighting = False


class AnvilWorldFolder(object):
    def __init__(self, filename):
        if not os.path.exists(filename):
            os.mkdir(filename)

        elif not os.path.isdir(filename):
            raise IOError("AnvilWorldFolder: Not a folder: %s" % filename)

        self.filename = filename
        self.regionFiles = {}

    # --- File paths ---

    def getFilePath(self, path):
        path = path.replace("/", os.path.sep)
        return os.path.join(self.filename, path)

    def getFolderPath(self, path, checksExists=True, generation=False):
        if checksExists and not os.path.exists(self.filename) and "##MCEDIT.TEMP##" in path and not generation:
            raise IOError("The file does not exist")
        path = self.getFilePath(path)
        if not os.path.exists(path) and "players" not in path:
            os.makedirs(path)

        return path

    # --- Region files ---

    def getRegionFilename(self, rx, rz):
        return os.path.join(self.getFolderPath("region", False), "r.%s.%s.%s" % (rx, rz, "mca"))

    def getRegionFile(self, rx, rz):
        regionFile = self.regionFiles.get((rx, rz))
        if regionFile:
            return regionFile
        regionFile = MCRegionFile(self.getRegionFilename(rx, rz), (rx, rz))
        self.regionFiles[rx, rz] = regionFile
        return regionFile

    def getRegionForChunk(self, cx, cz):
        rx = cx >> 5
        rz = cz >> 5
        return self.getRegionFile(rx, rz)

    def closeRegions(self):
        for rf in self.regionFiles.values():
            rf.close()

        self.regionFiles = {}

    # --- Chunks and chunk listing ---

    @staticmethod
    def tryLoadRegionFile(filepath):
        filename = os.path.basename(filepath)
        bits = filename.split('.')
        if len(bits) < 4 or bits[0] != 'r' or bits[3] != "mca":
            return None

        try:
            rx, rz = map(int, bits[1:3])
        except ValueError:
            return None

        return MCRegionFile(filepath, (rx, rz))

    def findRegionFiles(self):
        regionDir = self.getFolderPath("region", generation=True)

        regionFiles = os.listdir(regionDir)
        for filename in regionFiles:
            yield os.path.join(regionDir, filename)

    def listChunks(self):
        chunks = set()

        for filepath in self.findRegionFiles():
            regionFile = self.tryLoadRegionFile(filepath)
            if regionFile is None:
                continue

            if regionFile.offsets.any():
                rx, rz = regionFile.regionCoords
                self.regionFiles[rx, rz] = regionFile

                for index, offset in enumerate(regionFile.offsets):
                    if offset:
                        cx = index & 0x1f
                        cz = index >> 5

                        cx += rx << 5
                        cz += rz << 5

                        chunks.add((cx, cz))
            else:
                log.info(u"Removing empty region file {0}".format(filepath))
                regionFile.close()
                os.unlink(regionFile.path)

        return chunks

    def containsChunk(self, cx, cz):
        rx = cx >> 5
        rz = cz >> 5
        if not os.path.exists(self.getRegionFilename(rx, rz)):
            return False

        return self.getRegionForChunk(cx, cz).containsChunk(cx, cz)

    def deleteChunk(self, cx, cz):
        r = cx >> 5, cz >> 5
        rf = self.getRegionFile(*r)
        if rf:
            rf.setOffset(cx & 0x1f, cz & 0x1f, 0)
            if (rf.offsets == 0).all():
                rf.close()
                os.unlink(rf.path)
                del self.regionFiles[r]

    def readChunk(self, cx, cz):
        if not self.containsChunk(cx, cz):
            raise ChunkNotPresent((cx, cz))

        return self.getRegionForChunk(cx, cz).readChunk(cx, cz)

    def saveChunk(self, cx, cz, data):
        regionFile = self.getRegionForChunk(cx, cz)
        regionFile.saveChunk(cx, cz, data)

    def copyChunkFrom(self, worldFolder, cx, cz):
        fromRF = worldFolder.getRegionForChunk(cx, cz)
        rf = self.getRegionForChunk(cx, cz)
        rf.copyChunkFrom(fromRF, cx, cz)


class MCInfdevOldLevel(ChunkedLevelMixin, EntityLevel, PCMetadata):
    def __init__(self, filename=None, create=False, random_seed=None, last_played=None, readonly=False):
        """
        Load an Alpha level from the given filename. It can point to either
        a level.dat or a folder containing one. If create is True, it will
        also create the world using the random_seed and last_played arguments.
        If they are none, a random 64-bit seed will be selected for RandomSeed
        and long(time.time() * 1000) will be used for LastPlayed.

        If you try to create an existing world, its level.dat will be replaced.
        """

        super(MCInfdevOldLevel, self).__init__()
        self.Length = 0
        self.Width = 0
        self.Height = 256

        assert not (create and readonly)

        if os.path.basename(filename) in ("level.dat", "level.dat_old"):
            filename = os.path.dirname(filename)

        if not os.path.exists(filename):
            if not create:
                raise IOError('File not found')

            os.mkdir(filename)

        if not os.path.isdir(filename):
            raise IOError('File is not a Minecraft Alpha world')

        self.worldFolder = AnvilWorldFolder(filename)
        self.filename = self.worldFolder.getFilePath("level.dat")
        self.readonly = readonly
        if not readonly:
            self.acquireSessionLock()
            workFolderPath = self.worldFolder.getFolderPath("##MCEDIT.TEMP##")
            workFolderPath2 = self.worldFolder.getFolderPath("##MCEDIT.TEMP2##")
            if os.path.exists(workFolderPath):
                # xxxxxxx Opening a world a second time deletes the first world's work folder and crashes when the first
                # world tries to read a modified chunk from the work folder. This mainly happens when importing a world
                # into itself after modifying it.
                shutil.rmtree(workFolderPath, True)
            if os.path.exists(workFolderPath2):
                shutil.rmtree(workFolderPath2, True)

            self.unsavedWorkFolder = AnvilWorldFolder(workFolderPath)
            self.fileEditsFolder = AnvilWorldFolder(workFolderPath2)

            self.editFileNumber = 1

        # maps (cx, cz) pairs to AnvilChunk
        self._loadedChunks = weakref.WeakValueDictionary()

        # maps (cx, cz) pairs to AnvilChunkData
        self._loadedChunkData = {}
        self.recentChunks = collections.deque(maxlen=20)

        self.chunksNeedingLighting = set()
        self._allChunks = None
        self.dimensions = {}

        self.loadLevelDat(create, random_seed, last_played)

        assert self.version == self.VERSION_ANVIL, "Pre-Anvil world formats are not supported (for now)"

        if not readonly:
            self.initPlayers()
            self.preloadDimensions()

    def getFilePath(self, filename):
        return self.worldFolder.getFilePath(filename)

    def getFolderPath(self, dirname):
        return self.worldFolder.getFolderPath(dirname)

    # --- Load, save, create ---

    def saveInPlaceGen(self):
        if self.readonly:
            raise IOError("World is opened read only.")
        self.saving = True
        self.checkSessionLock()

        for level in self.dimensions.itervalues():
            for _ in MCInfdevOldLevel.saveInPlaceGen(level):
                yield

        dirtyChunkCount = 0
        for chunk in self._loadedChunkData.itervalues():
            cx, cz = chunk.chunkPosition
            if chunk.dirty:
                data = chunk.savedTagData()
                dirtyChunkCount += 1
                self.worldFolder.saveChunk(cx, cz, data)
                chunk.dirty = False
            yield

        for cx, cz in self.unsavedWorkFolder.listChunks():
            if (cx, cz) not in self._loadedChunkData:
                data = self.unsavedWorkFolder.readChunk(cx, cz)
                self.worldFolder.saveChunk(cx, cz, data)
                dirtyChunkCount += 1
            yield

        self.unsavedWorkFolder.closeRegions()
        shutil.rmtree(self.unsavedWorkFolder.filename, True)
        if not os.path.exists(self.unsavedWorkFolder.filename):
            os.mkdir(self.unsavedWorkFolder.filename)

        self.save_metadata()
        self.saving = False
        log.info(u"Saved {0} chunks (dim {1})".format(dirtyChunkCount, self.dimNo))

    def unload(self):
        """
        Unload all chunks and close all open filehandles.
        """
        if self.saving:
            raise ChunkAccessDenied
        self.worldFolder.closeRegions()
        if not self.readonly:
            self.unsavedWorkFolder.closeRegions()

        self._allChunks = None
        self.recentChunks.clear()
        self._loadedChunks.clear()
        self._loadedChunkData.clear()

    def close(self):
        """
        Unload all chunks and close all open filehandles. Discard any unsaved data.
        """
        self.unload()
        try:
            self.checkSessionLock()
            shutil.rmtree(self.unsavedWorkFolder.filename, True)
            shutil.rmtree(self.fileEditsFolder.filename, True)
        except SessionLockLost:
            pass

    # --- Resource limits ---

    loadedChunkLimit = 400

    # --- Constants ---

    GAMETYPE_SURVIVAL = 0
    GAMETYPE_CREATIVE = 1

    VERSION_MCR = 19132
    VERSION_ANVIL = 19133

    # --- Instance variables  ---

    materials = alphaMaterials
    isInfinite = True
    parentWorld = None
    dimNo = 0
    Height = 256
    _bounds = None

    # --- World info ---

    def __str__(self):
        return "MCInfdevOldLevel(\"%s\")" % os.path.basename(self.worldFolder.filename)

    @property
    def displayName(self):
        # shortname = os.path.basename(self.filename)
        # if shortname == "level.dat":
        shortname = os.path.basename(os.path.dirname(self.filename))

        return shortname

    @property
    def bounds(self):
        if self._bounds is None:
            self._bounds = self.getWorldBounds()
        return self._bounds

    def getWorldBounds(self):
        if self.chunkCount == 0:
            return BoundingBox((0, 0, 0), (0, 0, 0))

        allChunks = array(list(self.allChunks))
        mincx = (allChunks[:, 0]).min()
        maxcx = (allChunks[:, 0]).max()
        mincz = (allChunks[:, 1]).min()
        maxcz = (allChunks[:, 1]).max()

        origin = (mincx << 4, 0, mincz << 4)
        size = ((maxcx - mincx + 1) << 4, self.Height, (maxcz - mincz + 1) << 4)

        return BoundingBox(origin, size)

    @property
    def size(self):
        return self.bounds.size

    # --- Format detection ---

    @classmethod
    def _isLevel(cls, filename):

        if os.path.exists(os.path.join(filename, "chunks.dat")) or os.path.exists(os.path.join(filename, "db")):
            return False  # exclude Pocket Edition folders

        if not os.path.isdir(filename):
            f = os.path.basename(filename)
            if f not in ("level.dat", "level.dat_old"):
                return False
            filename = os.path.dirname(filename)

        files = os.listdir(filename)
        if "db" in files:
            return False
        if "level.dat" in files or "level.dat_old" in files:
            return True

        return False

    # --- Dimensions ---

    def preloadDimensions(self):
        worldDirs = os.listdir(self.worldFolder.filename)

        for dirname in worldDirs:
            if dirname.startswith("DIM"):
                try:
                    dimNo = int(dirname[3:])
                    log.info("Found dimension {0}".format(dirname))
                    dim = MCAlphaDimension(self, dimNo)
                    self.dimensions[dimNo] = dim
                except Exception, e:
                    log.error(u"Error loading dimension {0}: {1}".format(dirname, e))

    def getDimension(self, dimNo):
        if self.dimNo != 0:
            return self.parentWorld.getDimension(dimNo)

        if dimNo in self.dimensions:
            return self.dimensions[dimNo]
        dim = MCAlphaDimension(self, dimNo, create=True)
        self.dimensions[dimNo] = dim
        return dim

    # --- Region I/O ---

    def preloadChunkPositions(self):
        log.info(u"Scanning for regions...")
        self._allChunks = self.worldFolder.listChunks()
        if not self.readonly:
            self._allChunks.update(self.unsavedWorkFolder.listChunks())
        self._allChunks.update(self._loadedChunkData.iterkeys())

    def getRegionForChunk(self, cx, cz):
        return self.worldFolder.getRegionForChunk(cx, cz)

    # --- Chunk I/O ---

    def dirhash(self, n):
        return self.dirhashes[n % 64]

    def _dirhash(self):
        n = self
        n %= 64
        s = u""
        if n >= 36:
            s += u"1"
            n -= 36
        s += u"0123456789abcdefghijklmnopqrstuvwxyz"[n]

        return s

    dirhashes = [_dirhash(n) for n in range(64)]

    def _oldChunkFilename(self, cx, cz):
        return self.worldFolder.getFilePath(
            "%s/%s/c.%s.%s.dat" % (self.dirhash(cx), self.dirhash(cz), base36(cx), base36(cz)))

    def extractChunksInBox(self, box, parentFolder):
        for cx, cz in box.chunkPositions:
            if self.containsChunk(cx, cz):
                self.extractChunk(cx, cz, parentFolder)

    def extractChunk(self, cx, cz, parentFolder):
        if not os.path.exists(parentFolder):
            os.mkdir(parentFolder)

        chunkFilename = self._oldChunkFilename(cx, cz)
        outputFile = os.path.join(parentFolder, os.path.basename(chunkFilename))

        chunk = self.getChunk(cx, cz)

        chunk.root_tag.save(outputFile)

    @property
    def chunkCount(self):
        """Returns the number of chunks in the level. May initiate a costly
        chunk scan."""
        if self._allChunks is None:
            self.preloadChunkPositions()
        return len(self._allChunks)

    @property
    def allChunks(self):
        """Iterates over (xPos, zPos) tuples, one for each chunk in the level.
        May initiate a costly chunk scan."""
        if self._allChunks is None:
            self.preloadChunkPositions()
        return self._allChunks.__iter__()

    def copyChunkFrom(self, world, cx, cz):
        """
        Copy a chunk from world into the same chunk position in self.
        """
        assert isinstance(world, MCInfdevOldLevel)
        if self.readonly:
            raise IOError("World is opened read only.")
        if world.saving | self.saving:
            raise ChunkAccessDenied
        self.checkSessionLock()

        destChunk = self._loadedChunks.get((cx, cz))
        sourceChunk = world._loadedChunks.get((cx, cz))

        if sourceChunk:
            if destChunk:
                log.debug("Both chunks loaded. Using block copy.")
                # Both chunks loaded. Use block copy.
                self.copyBlocksFrom(world, destChunk.bounds, destChunk.bounds.origin)
                return
            else:
                log.debug("Source chunk loaded. Saving into work folder.")

                # Only source chunk loaded. Discard destination chunk and save source chunk in its place.
                self._loadedChunkData.pop((cx, cz), None)
                self.unsavedWorkFolder.saveChunk(cx, cz, sourceChunk.savedTagData())
                return
        else:
            if destChunk:
                log.debug("Destination chunk loaded. Using block copy.")
                # Only destination chunk loaded. Use block copy.
                self.copyBlocksFrom(world, destChunk.bounds, destChunk.bounds.origin)
            else:
                log.debug("No chunk loaded. Using world folder.copyChunkFrom")
                # Neither chunk loaded. Copy via world folders.
                self._loadedChunkData.pop((cx, cz), None)

                # If the source chunk is dirty, write it to the work folder.
                chunkData = world._loadedChunkData.pop((cx, cz), None)
                if chunkData and chunkData.dirty:
                    data = chunkData.savedTagData()
                    world.unsavedWorkFolder.saveChunk(cx, cz, data)

                if world.unsavedWorkFolder.containsChunk(cx, cz):
                    sourceFolder = world.unsavedWorkFolder
                else:
                    sourceFolder = world.worldFolder

                self.unsavedWorkFolder.copyChunkFrom(sourceFolder, cx, cz)

    def _getChunkBytes(self, cx, cz):
        if not self.readonly and self.unsavedWorkFolder.containsChunk(cx, cz):
            return self.unsavedWorkFolder.readChunk(cx, cz)
        else:
            return self.worldFolder.readChunk(cx, cz)

    def _getChunkData(self, cx, cz):
        chunkData = self._loadedChunkData.get((cx, cz))
        if chunkData is not None:
            return chunkData

        if self.saving:
            raise ChunkAccessDenied

        try:
            data = self._getChunkBytes(cx, cz)
            root_tag = nbt.load(buf=data)
            chunkData = AnvilChunkData(self, (cx, cz), root_tag)
        except (MemoryError, ChunkNotPresent):
            raise
        except Exception, e:
            raise ChunkMalformed("Chunk {0} had an error: {1!r}".format((cx, cz), e), sys.exc_info()[2])

        if not self.readonly and self.unsavedWorkFolder.containsChunk(cx, cz):
            chunkData.dirty = True

        self._storeLoadedChunkData(chunkData)

        return chunkData

    def _storeLoadedChunkData(self, chunkData):
        if len(self._loadedChunkData) > self.loadedChunkLimit:
            # Try to find a chunk to unload. The chunk must not be in _loadedChunks, which contains only chunks that
            # are in use by another object. If the chunk is dirty, save it to the temporary folder.
            if not self.readonly:
                self.checkSessionLock()
            for (ocx, ocz), oldChunkData in self._loadedChunkData.items():
                if (ocx, ocz) not in self._loadedChunks:
                    if oldChunkData.dirty and not self.readonly:
                        data = oldChunkData.savedTagData()
                        self.unsavedWorkFolder.saveChunk(ocx, ocz, data)

                    del self._loadedChunkData[ocx, ocz]
                    break

        self._loadedChunkData[chunkData.chunkPosition] = chunkData

    def getChunk(self, cx, cz):
        """ read the chunk from disk, load it, and return it."""

        chunk = self._loadedChunks.get((cx, cz))
        if chunk is not None:
            return chunk

        chunkData = self._getChunkData(cx, cz)
        chunk = AnvilChunk(chunkData)

        self._loadedChunks[cx, cz] = chunk
        self.recentChunks.append(chunk)
        return chunk

    def markDirtyChunk(self, cx, cz):
        self.getChunk(cx, cz).chunkChanged()

    def markDirtyBox(self, box):
        for cx, cz in box.chunkPositions:
            self.markDirtyChunk(cx, cz)

    def listDirtyChunks(self):
        for cPos, chunkData in self._loadedChunkData.iteritems():
            if chunkData.dirty:
                yield cPos

    # --- HeightMaps ---

    def heightMapAt(self, x, z):
        zc = z >> 4
        xc = x >> 4
        xInChunk = x & 0xf
        zInChunk = z & 0xf

        ch = self.getChunk(xc, zc)

        heightMap = ch.HeightMap

        return heightMap[zInChunk, xInChunk]  # HeightMap indices are backwards

    # --- Biome manipulation ---

    def biomeAt(self, x, z):
        biomes = self.getChunk(int(x/16),int(z/16)).root_tag["Level"]["Biomes"].value
        xChunk = int(x/16) * 16
        zChunk = int(z/16) * 16
        return biomes[(z - zChunk) * 16 + (x - xChunk)]

    def setBiomeAt(self, x, z, biomeID):
        biomes = self.getChunk(int(x/16), int(z/16)).root_tag["Level"]["Biomes"].value
        xChunk = int(x/16) * 16
        zChunk = int(z/16) * 16
        biomes[(z - zChunk) * 16 + (x - xChunk)] = biomeID

    # --- Entities and TileEntities ---

    def addEntity(self, entityTag):
        assert isinstance(entityTag, nbt.TAG_Compound)
        x, y, z = map(lambda x: int(floor(x)), Entity.pos(entityTag))

        try:
            chunk = self.getChunk(x >> 4, z >> 4)
        except (ChunkNotPresent, ChunkMalformed):
            return None
            # raise Error, can't find a chunk?
        chunk.addEntity(entityTag)
        chunk.dirty = True

    def tileEntityAt(self, x, y, z):
        chunk = self.getChunk(x >> 4, z >> 4)
        return chunk.tileEntityAt(x, y, z)

    def addTileEntity(self, tileEntityTag):
        assert isinstance(tileEntityTag, nbt.TAG_Compound)
        if 'x' not in tileEntityTag:
            return
        x, y, z = TileEntity.pos(tileEntityTag)

        try:
            chunk = self.getChunk(x >> 4, z >> 4)
        except (ChunkNotPresent, ChunkMalformed):
            return
            # raise Error, can't find a chunk?
        chunk.addTileEntity(tileEntityTag)
        chunk.dirty = True

    def addTileTick(self, tickTag):
        assert isinstance(tickTag, nbt.TAG_Compound)

        if 'x' not in tickTag:
            return
        x, y, z = TileTick.pos(tickTag)
        try:
            chunk = self.getChunk(x >> 4,z >> 4)
        except(ChunkNotPresent, ChunkMalformed):
            return
        chunk.addTileTick(tickTag)
        chunk.dirty = True

    def getEntitiesInBox(self, box):
        entities = []
        for chunk, slices, point in self.getChunkSlices(box):
            entities += chunk.getEntitiesInBox(box)

        return entities

    def getTileEntitiesInBox(self, box):
        tileEntites = []
        for chunk, slices, point in self.getChunkSlices(box):
            tileEntites += chunk.getTileEntitiesInBox(box)

        return tileEntites

    def getTileTicksInBox(self, box):
        tileticks = []
        for chunk, slices, point in self.getChunkSlices(box):
            tileticks += chunk.getTileTicksInBox(box)

        return tileticks

    def removeEntitiesInBox(self, box):
        count = 0
        for chunk, slices, point in self.getChunkSlices(box):
            count += chunk.removeEntitiesInBox(box)

        log.info("Removed {0} entities".format(count))
        return count

    def removeTileEntitiesInBox(self, box):
        count = 0
        for chunk, slices, point in self.getChunkSlices(box):
            count += chunk.removeTileEntitiesInBox(box)

        log.info("Removed {0} tile entities".format(count))
        return count

    def removeTileTicksInBox(self, box):
        count = 0
        for chunk, slices, point in self.getChunkSlices(box):
            count += chunk.removeTileTicksInBox(box)

        log.info("Removed {0} tile ticks".format(count))
        return count

    # --- Chunk manipulation ---

    def containsChunk(self, cx, cz):
        if self._allChunks is not None:
            return (cx, cz) in self._allChunks
        if (cx, cz) in self._loadedChunkData:
            return True

        return self.worldFolder.containsChunk(cx, cz)

    def containsPoint(self, x, y, z):
        if y < 0 or y > 127:
            return False
        return self.containsChunk(x >> 4, z >> 4)

    def createChunk(self, cx, cz):
        if self.containsChunk(cx, cz):
            raise ValueError("{0}:Chunk {1} already present!".format(self, (cx, cz)))
        if self._allChunks is not None:
            self._allChunks.add((cx, cz))

        self._storeLoadedChunkData(AnvilChunkData(self, (cx, cz), create=True))
        self._bounds = None

    def createChunks(self, chunks):

        i = 0
        ret = []
        for cx, cz in chunks:
            i += 1
            if not self.containsChunk(cx, cz):
                ret.append((cx, cz))
                self.createChunk(cx, cz)
            assert self.containsChunk(cx, cz), "Just created {0} but it didn't take".format((cx, cz))
            if i % 100 == 0:
                log.info(u"Chunk {0}...".format(i))

        log.info("Created {0} chunks.".format(len(ret)))

        return ret

    def createChunksInBox(self, box):
        log.info(u"Creating {0} chunks in {1}".format((box.maxcx - box.mincx) * (box.maxcz - box.mincz),
                                                      ((box.mincx, box.mincz), (box.maxcx, box.maxcz))))
        return self.createChunks(box.chunkPositions)

    def deleteChunk(self, cx, cz):
        self.worldFolder.deleteChunk(cx, cz)
        if self._allChunks is not None:
            self._allChunks.discard((cx, cz))

        self._bounds = None

    def deleteChunksInBox(self, box):
        log.info(u"Deleting {0} chunks in {1}".format((box.maxcx - box.mincx) * (box.maxcz - box.mincz),
                                                      ((box.mincx, box.mincz), (box.maxcx, box.maxcz))))
        i = 0
        ret = []
        for cx, cz in itertools.product(xrange(box.mincx, box.maxcx), xrange(box.mincz, box.maxcz)):
            i += 1
            if self.containsChunk(cx, cz):
                self.deleteChunk(cx, cz)
                ret.append((cx, cz))

            assert not self.containsChunk(cx, cz), "Just deleted {0} but it didn't take".format((cx, cz))

            if i % 100 == 0:
                log.info(u"Chunk {0}...".format(i))

        return ret


class MCAlphaDimension(MCInfdevOldLevel):
    def __init__(self, parentWorld, dimNo, create=False):
        filename = parentWorld.worldFolder.getFolderPath("DIM" + str(int(dimNo)))

        self.parentWorld = parentWorld
        MCInfdevOldLevel.__init__(self, filename, create)
        self.dimNo = dimNo
        self.filename = parentWorld.filename
        self.players = self.parentWorld.players
        self.playersFolder = self.parentWorld.playersFolder
        self.playerTagCache = self.parentWorld.playerTagCache

    @property
    def root_tag(self):
        return self.parentWorld.root_tag

    def __str__(self):
        return u"MCAlphaDimension({0}, {1})".format(self.parentWorld, self.dimNo)

    def loadLevelDat(self, create=False, random_seed=None, last_played=None):
        pass

    def preloadDimensions(self):
        pass

    def _create(self, *args, **kw):
        pass

    def acquireSessionLock(self):
        pass

    def checkSessionLock(self):
        self.parentWorld.checkSessionLock()

    dimensionNames = {-1: "Nether", 1: "The End"}

    @property
    def displayName(self):
        return u"{0} ({1})".format(self.parentWorld.displayName,
                                   self.dimensionNames.get(self.dimNo, "Dimension %d" % self.dimNo))

    def saveInPlace(self, saveSelf=False):
        """saving the dimension will save the parent world, which will save any
         other dimensions that need saving.  the intent is that all of them can
         stay loaded at once for fast switching """

        if saveSelf:
            MCInfdevOldLevel.saveInPlace(self)
        else:
            self.parentWorld.saveInPlace()
